import asyncio
import json
import logging

from redis.asyncio import Redis
from sqlalchemy import select

from .config import get_settings
from .db import session_factory
from .models import OutboxEvent, utcnow

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(self) -> None:
        self.redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        while not self.stopping.is_set():
            published = await self.publish_batch()
            if not published:
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=0.25)
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
                    .limit(100)
                )
            ).all()
            for event in events:
                await self.redis.xadd(
                    f"af:{event.topic}",
                    {
                        "event_id": str(event.id),
                        "aggregate_id": event.aggregate_id,
                        "payload": json.dumps(event.payload, separators=(",", ":")),
                    },
                    maxlen=100_000,
                    approximate=True,
                )
                event.published_at = utcnow()
            return len(events)

    async def close(self) -> None:
        self.stopping.set()
        await self.redis.aclose()
