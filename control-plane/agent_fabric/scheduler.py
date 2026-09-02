import asyncio
import hashlib
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
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

# All scheduler replicas use this transaction-scoped PostgreSQL advisory lock for
# the small correctness-critical commit phase. Candidate selection and placement
# planning remain parallel.
SCHEDULER_COMMIT_LOCK = 0x41465F5343484544


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
    gpu_count: int = 0
    vram_mb: int = 0
    free_gpu: int = 0
    free_vram: int = 0
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def fits(
        self,
        resources: dict[str, int],
        required_capabilities: list[str] | tuple[str, ...] = (),
    ) -> bool:
        return (
            self.free_cpu >= resources["cpu_millis"]
            and self.free_memory >= resources["memory_mb"]
            and self.free_pids >= resources["pids"]
            and self.free_gpu >= resources.get("gpu", 0)
            and self.free_vram >= resources.get("vram_mb", 0)
            and set(required_capabilities).issubset(self.capabilities)
        )

    def remaining_dominant(self, resources: dict[str, int]) -> float:
        cpu_left = (self.free_cpu - resources["cpu_millis"]) / self.cpu_millis
        memory_left = (self.free_memory - resources["memory_mb"]) / self.memory_mb
        dimensions = [cpu_left, memory_left]
        if resources.get("gpu", 0):
            dimensions.append((self.free_gpu - resources["gpu"]) / self.gpu_count)
        if resources.get("vram_mb", 0):
            dimensions.append((self.free_vram - resources["vram_mb"]) / self.vram_mb)
        return max(dimensions)

    def reserve(self, resources: dict[str, int]) -> None:
        self.free_cpu -= resources["cpu_millis"]
        self.free_memory -= resources["memory_mb"]
        self.free_pids -= resources["pids"]
        self.free_gpu -= resources.get("gpu", 0)
        self.free_vram -= resources.get("vram_mb", 0)


@dataclass
class Placement:
    run_id: uuid.UUID
    project_id: uuid.UUID
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

    Planning reads an unlocked capacity snapshot; the commit phase locks and revalidates
    only workers that were actually selected. Reservations are mutated on those locked
    ORM rows and flushed as a batch, so heartbeat flushes avoid broad or long-held locks.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.stopping = asyncio.Event()
        self.deficits: dict[uuid.UUID, float] = {}
        self.worker_cursor: str | None = None

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
            # This snapshot is only a fast-path optimization. The commit-locked recheck
            # below is authoritative, but avoiding an O(candidates * workers) plan when
            # another replica has filled the offer window is essential at high replica
            # counts.
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
                # Planning is deliberately outside this lock. The short commit phase
                # serializes the two global invariants that SKIP LOCKED cannot protect:
                # tenant running limits and the outstanding-offer ceiling.
                # Never wait for the commit lock while holding SKIP LOCKED candidate
                # rows. A waiting replica can otherwise close a Run -> advisory ->
                # Worker -> Run cycle with a concurrent acknowledgement and kill the
                # worker stream on PostgreSQL's deadlock victim path. Losing replicas
                # release their candidate rows at transaction exit and retry next poll.
                acquired = await session.scalar(
                    select(func.pg_try_advisory_xact_lock(SCHEDULER_COMMIT_LOCK))
                )
                if not acquired:
                    return 0
                outstanding = await session.scalar(
                    select(func.count())
                    .select_from(Attempt)
                    .where(Attempt.state == AttemptState.OFFERED)
                )
                OUTSTANDING_OFFERS.set(outstanding or 0)
                available = max(
                    0, self.settings.scheduler_max_outstanding_offers - (outstanding or 0)
                )
                exact_running = await self._running_counts(session)
                planned = placements
                placements = self._trim_for_commit(planned, projects, exact_running, available)
                placements = await self._trim_for_capacity(session, placements)
                accepted_ids = {placement.run_id for placement in placements}
                for placement in planned:
                    if placement.run_id not in accepted_ids:
                        self.deficits[placement.project_id] += 1.0
                if placements:
                    # Planning can be CPU-bound and multiple replicas can wait for this
                    # commit lock. Start the acknowledgement clock only once an offer is
                    # actually ready to become visible to the outbox.
                    expires = datetime.now(UTC) + timedelta(
                        seconds=self.settings.acknowledgement_deadline_seconds
                    )
                    for placement in placements:
                        placement.expires = expires
                        placement.payload["expires_unix_millis"] = int(expires.timestamp() * 1000)
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
        worker_filter = (Worker.draining.is_(False), Worker.last_seen_at >= cutoff)
        healthy = await session.scalar(
            select(func.count()).select_from(Worker).where(*worker_filter)
        )
        if self.worker_cursor is None:
            offset = secrets.randbelow(healthy) if healthy else 0
            self.worker_cursor = await session.scalar(
                select(Worker.id).where(*worker_filter).order_by(Worker.id).offset(offset).limit(1)
            )

        columns = (
            Worker.id,
            Worker.cpu_millis,
            Worker.memory_mb,
            Worker.pids,
            Worker.gpu_count,
            Worker.vram_mb,
            Worker.reserved_cpu_millis,
            Worker.reserved_memory_mb,
            Worker.reserved_pids,
            Worker.reserved_gpu_count,
            Worker.reserved_vram_mb,
            Worker.capabilities,
            Worker.sandbox_backends,
        )
        rows = []
        if self.worker_cursor is not None:
            rows = list(
                (
                    await session.execute(
                        select(*columns)
                        .where(*worker_filter, Worker.id > self.worker_cursor)
                        .order_by(Worker.id)
                        .limit(self.settings.scheduler_worker_limit)
                    )
                ).all()
            )
        remaining = self.settings.scheduler_worker_limit - len(rows)
        if remaining > 0:
            rows.extend(
                (
                    await session.execute(
                        select(*columns).where(*worker_filter).order_by(Worker.id).limit(remaining)
                    )
                ).all()
            )
        if rows:
            self.worker_cursor = rows[-1].id
        HEALTHY_WORKERS.set(healthy or 0)
        capacities = [
            Capacity(
                id=row.id,
                cpu_millis=row.cpu_millis,
                memory_mb=row.memory_mb,
                pids=row.pids,
                gpu_count=row.gpu_count,
                vram_mb=row.vram_mb,
                free_cpu=row.cpu_millis - row.reserved_cpu_millis,
                free_memory=row.memory_mb - row.reserved_memory_mb,
                free_pids=row.pids - row.reserved_pids,
                free_gpu=row.gpu_count - row.reserved_gpu_count,
                free_vram=row.vram_mb - row.reserved_vram_mb,
                capabilities=frozenset(row.capabilities),
            )
            for row in rows
            if "gvisor" in row.sandbox_backends
        ]
        # Equal-capacity workers must not be selected by lexicographic ID forever.
        # Shuffle only the bounded window; best-fit/resource scoring remains unchanged.
        secrets.SystemRandom().shuffle(capacities)
        return capacities

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
        running = await self._running_counts(session)
        return projects, running

    async def _running_counts(self, session: AsyncSession) -> dict[uuid.UUID, int]:
        running_rows = (
            await session.execute(
                select(Run.project_id, func.count())
                .where(
                    Run.state.in_([RunState.LEASED, RunState.RUNNING, RunState.CANCEL_REQUESTED])
                )
                .group_by(Run.project_id)
            )
        ).all()
        return {project_id: int(count) for project_id, count in running_rows}

    @staticmethod
    def _trim_for_commit(
        placements: list[Placement],
        projects: dict[uuid.UUID, Project],
        running: dict[uuid.UUID, int],
        available_offers: int,
    ) -> list[Placement]:
        """Apply exact global admission limits while holding the commit lock."""
        accepted: list[Placement] = []
        for placement in placements:
            project_id = placement.project_id
            project = projects[project_id]
            if len(accepted) >= available_offers:
                break
            if running.get(project_id, 0) >= project.max_running:
                continue
            accepted.append(placement)
            running[project_id] = running.get(project_id, 0) + 1
        return accepted

    async def _trim_for_capacity(
        self, session: AsyncSession, placements: list[Placement]
    ) -> list[Placement]:
        """Lock selected workers and discard placements invalidated since planning."""
        if not placements:
            return []
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.unhealthy_after_seconds)
        worker_ids = sorted({placement.worker_id for placement in placements})
        workers = (
            await session.scalars(
                select(Worker)
                .where(
                    Worker.id.in_(worker_ids),
                    Worker.draining.is_(False),
                    Worker.last_seen_at >= cutoff,
                )
                .order_by(Worker.id)
                .with_for_update()
            )
        ).all()
        worker_by_id = {worker.id: worker for worker in workers}
        capacities = {
            worker.id: Capacity(
                id=worker.id,
                cpu_millis=worker.cpu_millis,
                memory_mb=worker.memory_mb,
                pids=worker.pids,
                gpu_count=worker.gpu_count,
                vram_mb=worker.vram_mb,
                free_cpu=worker.cpu_millis - worker.reserved_cpu_millis,
                free_memory=worker.memory_mb - worker.reserved_memory_mb,
                free_pids=worker.pids - worker.reserved_pids,
                free_gpu=worker.gpu_count - worker.reserved_gpu_count,
                free_vram=worker.vram_mb - worker.reserved_vram_mb,
                capabilities=frozenset(worker.capabilities),
            )
            for worker in workers
            if "gvisor" in worker.sandbox_backends
        }
        accepted: list[Placement] = []
        for placement in placements:
            capacity = capacities.get(placement.worker_id)
            required = placement.payload.get("required_capabilities", [])
            if capacity is None or not capacity.fits(placement.resources, required):
                continue
            capacity.reserve(placement.resources)
            worker = worker_by_id[placement.worker_id]
            worker.reserved_cpu_millis += placement.resources["cpu_millis"]
            worker.reserved_memory_mb += placement.resources["memory_mb"]
            worker.reserved_pids += placement.resources["pids"]
            worker.reserved_gpu_count += placement.resources.get("gpu", 0)
            worker.reserved_vram_mb += placement.resources.get("vram_mb", 0)
            accepted.append(placement)
        return accepted

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
        batch_assignments: dict[str, int] = {}
        if limit is None:
            limit = self.settings.scheduler_batch_size
        for run in ordered:
            if len(placements) >= limit:
                break
            project = projects[run.project_id]
            if running.get(run.project_id, 0) >= project.max_running:
                continue
            resources = run.spec["resources"]
            required_capabilities = run.spec.get("required_capabilities", [])
            best: Capacity | None = None
            best_value = (0, 0, 0.0)
            for capacity in capacities:
                if not capacity.fits(resources, required_capabilities):
                    continue
                # Preserve scarce accelerators and spread a burst across eligible workers
                # before assigning a second offer to one stream. Within an assignment
                # round, retain best-fit packing by dominant remaining resource.
                value = (
                    int(resources.get("gpu", 0) == 0 and capacity.gpu_count > 0),
                    batch_assignments.get(capacity.id, 0),
                    capacity.remaining_dominant(resources),
                )
                if best is None or value < best_value:
                    best, best_value = capacity, value
            if best is None:
                continue
            best.reserve(resources)
            batch_assignments[best.id] = batch_assignments.get(best.id, 0) + 1
            running[run.project_id] = running.get(run.project_id, 0) + 1
            self.deficits[run.project_id] = self.deficits.get(run.project_id, 0.0) - 1.0
            raw_token = secrets.token_urlsafe(32)
            expires = now + timedelta(seconds=self.settings.acknowledgement_deadline_seconds)
            attempt_id = uuid.uuid4()
            placements.append(
                Placement(
                    run_id=run.id,
                    project_id=run.project_id,
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
        await session.flush()

    @staticmethod
    def _fits(worker: Worker, spec: dict[str, Any]) -> bool:
        resources = spec["resources"]
        return (
            "gvisor" in worker.sandbox_backends
            and worker.cpu_millis - worker.reserved_cpu_millis >= resources["cpu_millis"]
            and worker.memory_mb - worker.reserved_memory_mb >= resources["memory_mb"]
            and worker.pids - worker.reserved_pids >= resources["pids"]
            and (worker.gpu_count or 0) - (worker.reserved_gpu_count or 0)
            >= resources.get("gpu", 0)
            and (worker.vram_mb or 0) - (worker.reserved_vram_mb or 0)
            >= resources.get("vram_mb", 0)
            and set(spec.get("required_capabilities", [])).issubset(worker.capabilities)
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
            "cpu_millis": spec["resources"]["cpu_millis"],
            "memory_mb": spec["resources"]["memory_mb"],
            "pids": spec["resources"]["pids"],
            "disk_mb": spec["resources"]["disk_mb"],
            "timeout_seconds": spec["resources"]["timeout_seconds"],
            "network_policy": spec["network"],
            "gpu_count": spec["resources"].get("gpu", 0),
            "vram_mb": spec["resources"].get("vram_mb", 0),
            "required_capabilities": spec.get("required_capabilities", []),
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
        worker.reserved_gpu_count = max(0, worker.reserved_gpu_count - resources.get("gpu", 0))
        worker.reserved_vram_mb = max(0, worker.reserved_vram_mb - resources.get("vram_mb", 0))

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
