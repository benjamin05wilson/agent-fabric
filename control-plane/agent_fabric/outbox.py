import asyncio
import json
import logging

from prometheus_client import Counter, Gauge, start_http_server
from redis.asyncio import Redis
from sqlalchemy import select, update

from .config import get_settings
from .db import session_factory
from .models import OutboxEvent, utcnow
from .telemetry import configure_telemetry

logger = logging.getLogger(__name__)

OUTBOX_PUBLISHED = Counter("agent_fabric_outbox_published_total", "Outbox events published")
OUTBOX_LAG_SECONDS = Gauge(
    "agent_fabric_outbox_lag_seconds", "Age of the oldest event in the last published batch"
)


class OutboxPublisher:
    """Copy committed outbox rows to Redis Streams.

    Runs as its own process (``agent-fabric-outbox``). It used to live inside the
    API process, where a submission burst starved it for tens of seconds and lease
    offers expired before delivery; see benchmarks/reports for the measurement.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        logger.info("outbox publisher started")
        while not self.stopping.is_set():
            try:
                published = await self.publish_batch()
            except Exception:
                logger.exception("outbox batch failed")
                published = 0
            if not published:
                try:
                    await asyncio.wait_for(
                        self.stopping.wait(), timeout=self.settings.outbox_poll_seconds
                    )
                except TimeoutError:
                    pass

    async def publish_batch(self) -> int:
        async with session_factory() as session, session.begin():
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(self.settings.outbox_batch_size)
                )
            ).all()
            if not events:
                return 0
            # One Redis round trip for the whole batch; a duplicate publish after a crash
            # between XADD and COMMIT is tolerated by every consumer (at-least-once).
            pipeline = self.redis.pipeline(transaction=False)
            for event in events:
                pipeline.xadd(
                    f"af:{event.topic}",
                    {
                        "event_id": str(event.id),
                        "aggregate_id": event.aggregate_id,
                        "payload": json.dumps(event.payload, separators=(",", ":")),
                    },
                    maxlen=100_000,
                    approximate=True,
                )
            await pipeline.execute()
            now = utcnow()
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id.in_([event.id for event in events]))
                .values(published_at=now)
            )
            OUTBOX_PUBLISHED.inc(len(events))
            OUTBOX_LAG_SECONDS.set((now - events[0].created_at).total_seconds())
            return len(events)

    async def close(self) -> None:
        self.stopping.set()
        await self.redis.aclose()


async def _main() -> None:
    publisher = OutboxPublisher()
    try:
        await publisher.run()
    finally:
        await publisher.close()


def run() -> None:
    configure_telemetry("agent-fabric-outbox")
    if get_settings().metrics_port:
        start_http_server(get_settings().metrics_port or 0)
    asyncio.run(_main())
