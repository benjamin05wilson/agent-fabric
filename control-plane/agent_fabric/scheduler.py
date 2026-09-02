import asyncio
import hashlib
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import start_http_server
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import session_factory
from .metrics import (
    HEALTHY_WORKERS,
    LEASE_EXPIRATIONS,
    OUTSTANDING_OFFERS,
    PLACEMENTS,
    QUEUE_DEPTH,
    SCHEDULING_SECONDS,
)
from .models import Attempt, AttemptState, OutboxEvent, Project, Run, RunState, Worker
from .telemetry import configure_telemetry

logger = logging.getLogger(__name__)


@dataclass
class Capacity:
    """In-memory view of a worker's free capacity for the duration of one batch."""

    id: str
    cpu_millis: int
    memory_mb: int
    pids: int
    free_cpu: int
    free_memory: int
    free_pids: int

    def fits(self, resources: dict[str, int]) -> bool:
        return (
            self.free_cpu >= resources["cpu_millis"]
            and self.free_memory >= resources["memory_mb"]
            and self.free_pids >= resources["pids"]
        )

    def remaining_dominant(self, resources: dict[str, int]) -> float:
        cpu_left = (self.free_cpu - resources["cpu_millis"]) / self.cpu_millis
        memory_left = (self.free_memory - resources["memory_mb"]) / self.memory_mb
        return max(cpu_left, memory_left)

    def reserve(self, resources: dict[str, int]) -> None:
        self.free_cpu -= resources["cpu_millis"]
        self.free_memory -= resources["memory_mb"]
        self.free_pids -= resources["pids"]


@dataclass
class Placement:
    run_id: uuid.UUID
    worker_id: str
    attempt_number: int
    resources: dict[str, int]
    payload: dict[str, Any]
    token_hash: str
    expires: datetime


class Scheduler:
    """Batch scheduler.

    The baseline placed one run per transaction and re-read up to 500 queued runs, every
    healthy worker, and per-project running counts for each placement, which capped
    throughput at roughly 25 placements per second regardless of fleet size (see
    docs/benchmark-report.md). This version reads those inputs once per batch, places up
    to `scheduler_batch_size` runs against an in-memory capacity view, and writes the
    attempts, run transitions, worker reservations, and outbox events in bulk.

    Worker reservations are applied as relative updates so completion and expiry paths,
    which lock and clamp the same rows, stay correct without the scheduler holding
    worker row locks across the batch.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.stopping = asyncio.Event()
        self.deficits: dict[uuid.UUID, float] = {}

    async def run_forever(self) -> None:
        logger.info("scheduler started", extra={"batch_size": self.settings.scheduler_batch_size})
        while not self.stopping.is_set():
            started = time.perf_counter()
            try:
                await self.reconcile_expired_leases()
                placed = await self.schedule_batch()
                if placed:
                    SCHEDULING_SECONDS.observe(time.perf_counter() - started)
                    PLACEMENTS.inc(placed)
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
        return await self.schedule_batch() > 0

    async def schedule_batch(self) -> int:
        async with session_factory() as session, session.begin():
            queue_count = await session.scalar(
                select(func.count()).select_from(Run).where(Run.state == RunState.QUEUED)
            )
            QUEUE_DEPTH.set(queue_count or 0)
            if not queue_count:
                return 0
            # Backpressure: an offer only becomes work once the gateway has processed the
            # worker's acknowledgement. Without this bound the batch scheduler out-runs the
            # gateway, offers expire unseen, and retry-safe runs burn their attempts
            # (measured in docs/benchmark-report.md).
            outstanding = await session.scalar(
                select(func.count())
                .select_from(Attempt)
                .where(Attempt.state == AttemptState.OFFERED)
            )
            OUTSTANDING_OFFERS.set(outstanding or 0)
            limit = min(
                self.settings.scheduler_batch_size,
                self.settings.scheduler_max_outstanding_offers - (outstanding or 0),
            )
            if limit <= 0:
                return 0
            candidates = await self._candidates(session)
            if not candidates:
                return 0
            capacities = await self._capacities(session)
            if not capacities:
                return 0
            projects, running = await self._admission(session, candidates)
            placements = self._plan(candidates, capacities, projects, running, limit)
            if placements:
                await self._persist(session, placements)
            return len(placements)

    async def _candidates(self, session: AsyncSession) -> list[Run]:
        rows = (
            await session.scalars(
                select(Run)
                .where(Run.state == RunState.QUEUED)
                .order_by(Run.created_at)
                .limit(self.settings.scheduler_candidate_limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        return list(rows)

    async def _capacities(self, session: AsyncSession) -> list[Capacity]:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.unhealthy_after_seconds)
        rows = (
            await session.execute(
                select(
                    Worker.id,
                    Worker.cpu_millis,
                    Worker.memory_mb,
                    Worker.pids,
                    Worker.reserved_cpu_millis,
                    Worker.reserved_memory_mb,
                    Worker.reserved_pids,
                    Worker.sandbox_backends,
                ).where(Worker.draining.is_(False), Worker.last_seen_at >= cutoff)
            )
        ).all()
        HEALTHY_WORKERS.set(len(rows))
        return [
            Capacity(
                id=row.id,
                cpu_millis=row.cpu_millis,
                memory_mb=row.memory_mb,
                pids=row.pids,
                free_cpu=row.cpu_millis - row.reserved_cpu_millis,
                free_memory=row.memory_mb - row.reserved_memory_mb,
                free_pids=row.pids - row.reserved_pids,
            )
            for row in rows
            if "gvisor" in row.sandbox_backends
        ]

    async def _admission(
        self, session: AsyncSession, candidates: list[Run]
    ) -> tuple[dict[uuid.UUID, Project], dict[uuid.UUID, int]]:
        project_ids = {run.project_id for run in candidates}
        projects = {
            project.id: project
            for project in (
                await session.scalars(select(Project).where(Project.id.in_(project_ids)))
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
        running = {project_id: int(count) for project_id, count in running_rows}
        return projects, running

    def _plan(
        self,
        candidates: list[Run],
        capacities: list[Capacity],
        projects: dict[uuid.UUID, Project],
        running: dict[uuid.UUID, int],
        limit: int | None = None,
    ) -> list[Placement]:
        """Chooses (run, worker) pairs without touching the database."""
        candidates = [run for run in candidates if run.project_id in projects]
        for project_id, project in projects.items():
            self.deficits[project_id] = self.deficits.get(project_id, 0.0) + project.weight
        now = datetime.now(UTC)

        def score(run: Run) -> tuple[float, float]:
            age_bonus = min(9.0, (now - run.created_at).total_seconds() / 60.0)
            return (
                self.deficits.get(run.project_id, 0.0) + run.priority + age_bonus,
                -run.created_at.timestamp(),
            )

        ordered = sorted(candidates, key=score, reverse=True)
        placements: list[Placement] = []
        if limit is None:
            limit = self.settings.scheduler_batch_size
        for run in ordered:
            if len(placements) >= limit:
                break
            project = projects[run.project_id]
            if running.get(run.project_id, 0) >= project.max_running:
                continue
            resources = run.spec["resources"]
            best: Capacity | None = None
            best_value = 0.0
            for capacity in capacities:
                if not capacity.fits(resources):
                    continue
                value = capacity.remaining_dominant(resources)
                if best is None or value < best_value:
                    best, best_value = capacity, value
            if best is None:
                continue
            best.reserve(resources)
            running[run.project_id] = running.get(run.project_id, 0) + 1
            self.deficits[run.project_id] = self.deficits.get(run.project_id, 0.0) - 1.0
            raw_token = secrets.token_urlsafe(32)
            expires = now + timedelta(seconds=self.settings.acknowledgement_deadline_seconds)
            attempt_id = uuid.uuid4()
            placements.append(
                Placement(
                    run_id=run.id,
                    worker_id=best.id,
                    attempt_number=run.attempt_count + 1,
                    resources=resources,
                    payload=self._lease_payload(run, attempt_id, raw_token, expires),
                    token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                    expires=expires,
                )
            )
        return placements

    async def _persist(self, session: AsyncSession, placements: list[Placement]) -> None:
        attempts = [
            Attempt(
                id=uuid.UUID(placement.payload["attempt_id"]),
                run_id=placement.run_id,
                worker_id=placement.worker_id,
                number=placement.attempt_number,
                lease_token_hash=placement.token_hash,
                lease_expires_at=placement.expires,
            )
            for placement in placements
        ]
        session.add_all(attempts)
        session.add_all(
            OutboxEvent(
                topic=f"lease.offer.{placement.worker_id}",
                aggregate_id=str(placement.run_id),
                payload=placement.payload,
            )
            for placement in placements
        )
        await session.execute(
            update(Run),
            [
                {
                    "id": placement.run_id,
                    "state": RunState.LEASED,
                    "attempt_count": placement.attempt_number,
                }
                for placement in placements
            ],
        )
        reservations: dict[str, dict[str, int]] = {}
        for placement in placements:
            totals = reservations.setdefault(
                placement.worker_id, {"cpu_millis": 0, "memory_mb": 0, "pids": 0}
            )
            for key in totals:
                totals[key] += placement.resources[key]
        for worker_id, totals in reservations.items():
            await session.execute(
                update(Worker)
                .where(Worker.id == worker_id)
                .values(
                    reserved_cpu_millis=Worker.reserved_cpu_millis + totals["cpu_millis"],
                    reserved_memory_mb=Worker.reserved_memory_mb + totals["memory_mb"],
                    reserved_pids=Worker.reserved_pids + totals["pids"],
                )
            )
        await session.flush()

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
        self, run: Run, attempt_id: uuid.UUID, raw_token: str, expires: datetime
    ) -> dict[str, Any]:
        spec = run.spec
        return {
            "run_id": str(run.id),
            "attempt_id": str(attempt_id),
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
