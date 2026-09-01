import asyncio
import hashlib
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import start_http_server
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import session_factory
from .metrics import HEALTHY_WORKERS, LEASE_EXPIRATIONS, QUEUE_DEPTH, SCHEDULING_SECONDS
from .models import Attempt, AttemptState, OutboxEvent, Project, Run, RunState, Worker
from .telemetry import configure_telemetry

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.stopping = asyncio.Event()
        self.deficits: dict[uuid.UUID, float] = {}

    async def run_forever(self) -> None:
        logger.info("scheduler started")
        while not self.stopping.is_set():
            started = time.perf_counter()
            try:
                await self.reconcile_expired_leases()
                placed = await self.schedule_one()
                if placed:
                    SCHEDULING_SECONDS.observe(time.perf_counter() - started)
                    continue
            except Exception:
                logger.exception("scheduler iteration failed")
            try:
                await asyncio.wait_for(
                    self.stopping.wait(), timeout=self.settings.scheduler_poll_seconds
                )
            except TimeoutError:
                pass

    async def schedule_one(self) -> bool:
        async with session_factory() as session, session.begin():
            queue_count = await session.scalar(
                select(func.count()).select_from(Run).where(Run.state == RunState.QUEUED)
            )
            QUEUE_DEPTH.set(queue_count or 0)
            if not queue_count:
                return False

            run = await self._choose_run(session)
            if run is None:
                return False
            worker = await self._choose_worker(session, run.spec)
            if worker is None:
                return False

            worker = await session.scalar(
                select(Worker).where(Worker.id == worker.id).with_for_update()
            )
            if worker is None or not self._fits(worker, run.spec):
                return False
            run = await session.scalar(select(Run).where(Run.id == run.id).with_for_update())
            if run is None or run.state != RunState.QUEUED:
                return False

            resources = run.spec["resources"]
            worker.reserved_cpu_millis += resources["cpu_millis"]
            worker.reserved_memory_mb += resources["memory_mb"]
            worker.reserved_pids += resources["pids"]
            raw_token = secrets.token_urlsafe(32)
            expires = datetime.now(UTC) + timedelta(
                seconds=self.settings.acknowledgement_deadline_seconds
            )
            run.attempt_count += 1
            run.state = RunState.LEASED
            attempt = Attempt(
                run_id=run.id,
                worker_id=worker.id,
                number=run.attempt_count,
                lease_token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                lease_expires_at=expires,
            )
            session.add(attempt)
            await session.flush()
            session.add(
                OutboxEvent(
                    topic=f"lease.offer.{worker.id}",
                    aggregate_id=str(run.id),
                    payload=self._lease_payload(run, attempt, raw_token, expires),
                )
            )
            return True

    async def _choose_run(self, session: AsyncSession) -> Run | None:
        candidates = (
            await session.scalars(
                select(Run).where(Run.state == RunState.QUEUED).order_by(Run.created_at).limit(500)
            )
        ).all()
        if not candidates:
            return None
        projects = {
            project.id: project
            for project in (
                await session.scalars(
                    select(Project).where(Project.id.in_({run.project_id for run in candidates}))
                )
            ).all()
        }
        running_rows = (
            await session.execute(
                select(Run.project_id, func.count())
                .where(
                    Run.state.in_([RunState.LEASED, RunState.RUNNING, RunState.CANCEL_REQUESTED])
                )
                .group_by(Run.project_id)
            )
        ).all()
        running: dict[uuid.UUID, int] = {project_id: count for project_id, count in running_rows}
        eligible = [
            run
            for run in candidates
            if run.project_id in projects
            and running.get(run.project_id, 0) < projects[run.project_id].max_running
        ]
        if not eligible:
            return None
        for project_id, project in projects.items():
            self.deficits[project_id] = self.deficits.get(project_id, 0.0) + project.weight
        now = datetime.now(UTC)

        def score(run: Run) -> tuple[float, datetime]:
            age_bonus = min(9.0, (now - run.created_at).total_seconds() / 60.0)
            score_value = self.deficits.get(run.project_id, 0.0) + run.priority + age_bonus
            return (score_value, run.created_at)

        chosen = max(eligible, key=score)
        self.deficits[chosen.project_id] = self.deficits.get(chosen.project_id, 0.0) - 1.0
        return chosen

    async def _choose_worker(self, session: AsyncSession, spec: dict[str, Any]) -> Worker | None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.unhealthy_after_seconds)
        workers = (
            await session.scalars(
                select(Worker).where(
                    Worker.draining.is_(False),
                    Worker.last_seen_at >= cutoff,
                )
            )
        ).all()
        HEALTHY_WORKERS.set(len(workers))
        eligible = [worker for worker in workers if self._fits(worker, spec)]
        if not eligible:
            return None
        resources = spec["resources"]

        def remaining_dominant(worker: Worker) -> float:
            cpu_left = worker.cpu_millis - worker.reserved_cpu_millis - resources["cpu_millis"]
            memory_left = worker.memory_mb - worker.reserved_memory_mb - resources["memory_mb"]
            return float(max(cpu_left / worker.cpu_millis, memory_left / worker.memory_mb))

        return min(eligible, key=remaining_dominant)

    @staticmethod
    def _fits(worker: Worker, spec: dict[str, Any]) -> bool:
        resources = spec["resources"]
        return (
            "gvisor" in worker.sandbox_backends
            and worker.cpu_millis - worker.reserved_cpu_millis >= resources["cpu_millis"]
            and worker.memory_mb - worker.reserved_memory_mb >= resources["memory_mb"]
            and worker.pids - worker.reserved_pids >= resources["pids"]
        )

    def _lease_payload(
        self, run: Run, attempt: Attempt, raw_token: str, expires: datetime
    ) -> dict[str, Any]:
        spec = run.spec
        return {
            "run_id": str(run.id),
            "attempt_id": str(attempt.id),
            "lease_token": raw_token,
            "expires_unix_millis": int(expires.timestamp() * 1000),
            "repository_url": spec["repository"]["url"],
            "repository_ref": spec["repository"]["ref"],
            "argv": spec["argv"],
            "environment": spec["environment"],
            "profile": spec["profile"],
            "image_digest": self.settings.profile_images[spec["profile"]],
            **spec["resources"],
            "network_policy": spec["network"],
            "traceparent": spec.get("_trace_context", {}).get("traceparent", ""),
        }

    async def reconcile_expired_leases(self) -> int:
        now = datetime.now(UTC)
        count = 0
        async with session_factory() as session, session.begin():
            attempts = (
                await session.scalars(
                    select(Attempt)
                    .where(
                        Attempt.state.in_([AttemptState.OFFERED, AttemptState.RUNNING]),
                        Attempt.lease_expires_at < now,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(100)
                )
            ).all()
            for attempt in attempts:
                run = await session.scalar(
                    select(Run).where(Run.id == attempt.run_id).with_for_update()
                )
                worker = await session.scalar(
                    select(Worker).where(Worker.id == attempt.worker_id).with_for_update()
                )
                if run is None:
                    continue
                if worker is not None:
                    self._release(worker, run.spec)
                attempt.state = AttemptState.LOST
                attempt.finished_at = now
                acknowledged = attempt.acknowledged_at is not None
                if run.state == RunState.CANCEL_REQUESTED:
                    run.state = RunState.CANCELLED
                    outcome = "cancelled"
                elif not acknowledged or (run.retry_safe and run.attempt_count < run.max_attempts):
                    run.state = RunState.QUEUED
                    outcome = "requeued"
                    session.add(
                        OutboxEvent(
                            topic="run.ready",
                            aggregate_id=str(run.id),
                            payload={"run_id": str(run.id), "reason": "lease_expired"},
                        )
                    )
                else:
                    run.state = RunState.LOST
                    run.failure_code = "WORKER_LOST"
                    run.failure_message = "worker heartbeat stopped during uncertain execution"
                    run.finished_at = now
                    outcome = "lost"
                LEASE_EXPIRATIONS.labels(str(acknowledged).lower(), outcome).inc()
                count += 1
        return count

    @staticmethod
    def _release(worker: Worker, spec: dict[str, Any]) -> None:
        resources = spec["resources"]
        worker.reserved_cpu_millis = max(0, worker.reserved_cpu_millis - resources["cpu_millis"])
        worker.reserved_memory_mb = max(0, worker.reserved_memory_mb - resources["memory_mb"])
        worker.reserved_pids = max(0, worker.reserved_pids - resources["pids"])

    async def close(self) -> None:
        self.stopping.set()
        await self.redis.aclose()


async def _main() -> None:
    scheduler = Scheduler()
    try:
        await scheduler.run_forever()
    finally:
        await scheduler.close()


def run() -> None:
    configure_telemetry("agent-fabric-scheduler")
    if get_settings().metrics_port:
        start_http_server(get_settings().metrics_port or 0)
    asyncio.run(_main())
