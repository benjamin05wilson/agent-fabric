import asyncio
import json
import logging
import uuid

from prometheus_client import Counter, Gauge, start_http_server
from redis.asyncio import Redis
from sqlalchemy import select, update

from .config import get_settings
from .db import session_factory
from .models import Attempt, AttemptState, OutboxEvent, utcnow
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
            # Resolve worker-directed events once per outbox batch. Gateways own a
            # single shard stream each, replacing one blocking Redis reader per worker.
            routes: dict[uuid.UUID, tuple[str, str, dict[str, object]]] = {}
            cancel_events = [event for event in events if event.topic == "run.cancel"]
            if cancel_events:
                run_ids = [uuid.UUID(event.aggregate_id) for event in cancel_events]
                attempts = (
                    await session.execute(
                        select(Attempt.run_id, Attempt.id, Attempt.worker_id).where(
                            Attempt.run_id.in_(run_ids),
                            Attempt.state.in_([AttemptState.OFFERED, AttemptState.RUNNING]),
                        )
                    )
                ).all()
                attempts_by_run = {row.run_id: row for row in attempts}
                for event in cancel_events:
                    attempt = attempts_by_run.get(uuid.UUID(event.aggregate_id))
                    if attempt is not None:
                        routes[event.id] = (
                            attempt.worker_id,
                            "cancel",
                            {"run_id": event.aggregate_id, "attempt_id": str(attempt.id)},
                        )
            for event in events:
                if event.topic.startswith("lease.offer."):
                    routes[event.id] = (
                        event.topic.removeprefix("lease.offer."),
                        "lease",
                        event.payload,
                    )

            worker_ids = sorted({route[0] for route in routes.values()})
            owner_values = (
                await self.redis.hmget("af:worker:owners", worker_ids) if worker_ids else []
            )
            owners = dict(zip(worker_ids, owner_values, strict=True))

            # One Redis round trip for the whole batch; a duplicate publish after a crash
            # between XADD and COMMIT is tolerated by every consumer (at-least-once).
            pipeline = self.redis.pipeline(transaction=False)
            published_ids: list[uuid.UUID] = []
            for event in events:
                route = routes.get(event.id)
                if route is not None:
                    worker_id, kind, payload = route
                    owner = owners.get(worker_id)
                    if not owner:
                        # Registration commits before the gateway announces ownership.
                        # Keep the durable event unpublished and retry next batch.
                        continue
                    stream = f"af:gateway:{owner}:outbound"
                    fields = {
                        "event_id": str(event.id),
                        "worker_id": worker_id,
                        "kind": kind,
                        "payload": json.dumps(payload, separators=(",", ":")),
                    }
                elif event.topic == "run.cancel":
                    # No active attempt remains, so cancellation delivery is obsolete.
                    published_ids.append(event.id)
                    continue
                else:
                    stream = f"af:{event.topic}"
                    fields = {
                        "event_id": str(event.id),
                        "aggregate_id": event.aggregate_id,
                        "payload": json.dumps(event.payload, separators=(",", ":")),
                    }
                pipeline.xadd(stream, fields, maxlen=100_000, approximate=True)
                published_ids.append(event.id)
            await pipeline.execute()
            now = utcnow()
            if published_ids:
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id.in_(published_ids))
                    .values(published_at=now)
                )
                OUTBOX_PUBLISHED.inc(len(published_ids))
                OUTBOX_LAG_SECONDS.set((now - events[0].created_at).total_seconds())
            return len(published_ids)

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
