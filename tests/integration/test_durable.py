"""Small PostgreSQL/Redis regressions. Enable only in the isolated demo stack."""

import asyncio
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from agent_fabric import grpc_server, outbox, scheduler
from agent_fabric.config import Settings
from agent_fabric.models import (
    Attempt,
    AttemptState,
    Base,
    OutboxEvent,
    Project,
    Run,
    RunState,
    Worker,
    utcnow,
)
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    os.getenv("AF_SERVICE_TESTS") != "1",
    reason="requires isolated PostgreSQL/Redis; scripts/demo.py --regressions",
)


@pytest.fixture
async def store(monkeypatch):
    # No truncation of shared tables. A fresh schema isolates every test, including
    # multiple pytest processes. Redis cleanup removes only this test's route/stream.
    schema = "af_test_" + uuid.uuid4().hex
    admin = create_async_engine(os.environ["DATABASE_URL"])
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        os.environ["DATABASE_URL"], connect_args={"server_settings": {"search_path": schema}}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        redis_url=os.environ["REDIS_URL"],
        gateway_id=schema,
        scheduler_candidate_limit=1,
        scheduler_batch_size=1,
        scheduler_max_outstanding_offers=1,
    )
    for module in (scheduler, grpc_server, outbox):
        monkeypatch.setattr(module, "session_factory", factory)
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    gateway = grpc_server.WorkerControlService()
    publisher = outbox.OutboxPublisher()
    planners = [scheduler.Scheduler(), scheduler.Scheduler()]
    try:
        async with asyncio.timeout(30):
            yield SimpleNamespace(
                factory=factory,
                settings=settings,
                gateway=gateway,
                publisher=publisher,
                planners=planners,
                worker_id=schema,
            )
    finally:
        await gateway.redis.hdel("af:worker:owners", schema)
        await gateway.redis.delete(f"af:gateway:{schema}:outbound")
        await gateway.close()
        await publisher.close()
        for planner in planners:
            await planner.close()
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def seed(store, *, jobs=1, capacity=100, tenant_limit=1):
    async with store.factory() as session, session.begin():
        project = Project(slug="test", api_key_hash=b"0" * 32, max_running=tenant_limit)
        session.add(project)
        await session.flush()
        session.add(
            Worker(
                id=store.worker_id,
                protocol_version="v1",
                worker_version="test",
                cpu_millis=capacity,
                memory_mb=4096,
                pids=512,
                gpu_count=1,
                vram_mb=1024,
                capabilities=[],
                sandbox_backends=["gvisor"],
            )
        )
        for number in range(jobs):
            session.add(
                Run(
                    project_id=project.id,
                    idempotency_key=str(number),
                    request_hash="test",
                    retry_safe=True,
                    max_attempts=2,
                    spec={
                        "repository": {
                            "url": "https://github.com/octocat/Hello-World",
                            "ref": "master",
                        },
                        "argv": ["true"],
                        "environment": {},
                        "profile": "python",
                        "network": "disabled",
                        "resources": {
                            "cpu_millis": 100,
                            "memory_mb": 128,
                            "pids": 16,
                            "gpu": 1,
                            "vram_mb": 128,
                            "disk_mb": 128,
                            "timeout_seconds": 10,
                        },
                    },
                )
            )


async def assert_accounting(store, active):
    async with store.factory() as session:
        worker = await session.get(Worker, store.worker_id)
        assert (
            worker.reserved_cpu_millis,
            worker.reserved_memory_mb,
            worker.reserved_pids,
            worker.reserved_gpu_count,
            worker.reserved_vram_mb,
        ) == (100 * active, 128 * active, 16 * active, active, 128 * active)
        attempts = (
            await session.scalars(
                select(Attempt).where(
                    Attempt.state.in_([AttemptState.OFFERED, AttemptState.RUNNING])
                )
            )
        ).all()
        assert len(attempts) == active
        assert len({attempt.run_id for attempt in attempts}) == active


@pytest.mark.parametrize(
    "capacity,tenant_limit,offer_limit", [(100, 20, 20), (1000, 1, 20), (1000, 20, 1)]
)
async def test_two_real_transactions_cannot_overbook(
    store, monkeypatch, capacity, tenant_limit, offer_limit
):
    await seed(store, jobs=2, capacity=capacity, tenant_limit=tenant_limit)
    # Give ample GPU capacity so each parameter isolates its named limiting budget.
    async with store.factory() as session, session.begin():
        await session.execute(update(Worker).values(gpu_count=10))
    store.settings.scheduler_max_outstanding_offers = offer_limit
    barrier = asyncio.Barrier(2)
    original = scheduler.Scheduler._admission

    async def meet(self, session, candidates):
        result = await original(self, session, candidates)
        await barrier.wait()  # Both transactions hold different queued rows and stale snapshots.
        return result

    monkeypatch.setattr(scheduler.Scheduler, "_admission", meet)
    assert sum(await asyncio.gather(*(p.schedule_batch() for p in store.planners))) == 1
    monkeypatch.setattr(scheduler.Scheduler, "_admission", original)
    assert await store.planners[0].schedule_batch() == 0
    await assert_accounting(store, 1)
    async with store.factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Run).where(Run.state == RunState.LEASED)
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


async def lease(store):
    assert await store.planners[0].schedule_batch() == 1
    async with store.factory() as session:
        events = (
            await session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.topic.startswith("lease.offer."))
                .order_by(OutboxEvent.created_at)
            )
        ).all()
        return events[-1].payload


def acknowledgement(payload):
    return SimpleNamespace(
        attempt_id=payload["attempt_id"], lease_token=payload["lease_token"], accepted=True
    )


def completion(payload):
    return SimpleNamespace(
        attempt_id=payload["attempt_id"],
        lease_token=payload["lease_token"],
        terminal_state="SUCCEEDED",
        exit_code=0,
    )


async def test_outbox_replay_after_publish_before_commit(store, monkeypatch):
    await seed(store)
    payload = await lease(store)
    assert await store.publisher.publish_batch() == 0  # no owner: keep durable row
    await store.gateway.redis.hset("af:worker:owners", store.worker_id, store.settings.gateway_id)
    original_pipeline = store.publisher.redis.pipeline

    class CrashAfterPublish:
        def __init__(self):
            self.inner = original_pipeline(transaction=False)

        def xadd(self, *args, **kwargs):
            self.inner.xadd(*args, **kwargs)

        async def execute(self):
            await self.inner.execute()  # real Redis XADD; the PostgreSQL transaction rolls back
            raise RuntimeError("injected crash before database commit")

    monkeypatch.setattr(store.publisher.redis, "pipeline", lambda **kwargs: CrashAfterPublish())
    with pytest.raises(RuntimeError, match="injected crash"):
        await store.publisher.publish_batch()
    async with store.factory() as session:
        assert (await session.scalar(select(OutboxEvent))).published_at is None
    monkeypatch.setattr(store.publisher.redis, "pipeline", original_pipeline)
    assert await store.publisher.publish_batch() == 1
    stream = f"af:gateway:{store.settings.gateway_id}:outbound"
    entries = await store.gateway.redis.xrange(stream)
    assert len(entries) == 2 and entries[0][1]["event_id"] == entries[1][1]["event_id"]
    await store.gateway.redis.xgroup_create(stream, "test", id="0")
    await store.gateway.redis.xreadgroup("test", "consumer", {stream: ">"}, count=2)
    outgoing = asyncio.Queue()
    store.gateway.connections[store.worker_id] = outgoing
    for entry_id, fields in entries:
        assert await store.gateway._deliver_outbound(stream, "test", entry_id, fields)
        message = outgoing.get_nowait()
        assert message.lease.attempt_id == payload["attempt_id"]
        await store.gateway._acknowledge(store.worker_id, acknowledgement(payload))
    await assert_accounting(store, 1)
    await asyncio.gather(
        *(store.gateway._complete(store.worker_id, completion(payload)) for _ in range(2))
    )
    await assert_accounting(store, 0)
    async with store.factory() as session:
        assert (await session.scalar(select(Run))).state == RunState.SUCCEEDED
        assert await session.scalar(select(func.count()).select_from(Attempt)) == 1
    assert await store.gateway.redis.xlen(stream) == 0


async def test_retry_safe_loss_releases_once_and_ignores_stale_completion(store):
    await seed(store)
    payload = await lease(store)
    await store.gateway._acknowledge(store.worker_id, acknowledgement(payload))
    async with store.factory() as session, session.begin():
        await session.execute(
            update(Attempt).values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )
    assert sum(await asyncio.gather(*(p.reconcile_expired_leases() for p in store.planners))) == 1
    await assert_accounting(store, 0)
    async with store.factory() as session:
        assert (await session.scalar(select(Run))).state == RunState.QUEUED
        assert (await session.scalar(select(Attempt))).state == AttemptState.LOST
    retry = await lease(store)
    assert retry["attempt_id"] != payload["attempt_id"]
    await store.gateway._complete(store.worker_id, completion(payload))
    await assert_accounting(store, 1)
    await store.gateway._acknowledge(store.worker_id, acknowledgement(retry))
    await store.gateway._complete(store.worker_id, completion(retry))
    assert await store.planners[0].reconcile_expired_leases() == 0
    await assert_accounting(store, 0)
    async with store.factory() as session:
        run = await session.scalar(select(Run))
        assert run.state == RunState.SUCCEEDED and run.attempt_count == 2
        assert set(await session.scalars(select(Attempt.state))) == {
            AttemptState.LOST,
            AttemptState.SUCCEEDED,
        }
