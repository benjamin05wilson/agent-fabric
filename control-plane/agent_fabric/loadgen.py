"""Simulated worker fleet, job submitter, chaos injector, and durable-state auditor.

The generator drives the real control plane through the same gRPC and HTTP
contracts a Go worker and an API client use. It measures what the client can
see (submission, lease receipt, completion) and, when a database URL is given,
audits what PostgreSQL recorded so that requeues, losses, and leaked
reservations are counted from authoritative state instead of client memory.
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import grpc
import httpx

from .generated import worker_pb2, worker_pb2_grpc

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "LOST")


def percentiles(values: list[float]) -> dict[str, float | None]:
    ordered = sorted(values)

    def at(fraction: float) -> float | None:
        if not ordered:
            return None
        index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
        return round(ordered[index], 3)

    return {
        "count": len(ordered),
        "p50": at(0.50),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": round(ordered[-1], 3) if ordered else None,
        "mean": round(statistics.mean(ordered), 3) if ordered else None,
    }


@dataclass
class Measurements:
    target_workers: int
    started_wall: float = field(default_factory=time.time)
    started: float = field(default_factory=time.perf_counter)
    registered: int = 0
    registered_after_seconds: float | None = None
    leases: int = 0
    completed: int = 0
    stream_errors: int = 0
    lease_latencies_ms: list[float] = field(default_factory=list)
    submitted_at: dict[str, float] = field(default_factory=dict)
    submission: dict[str, Any] = field(default_factory=dict)
    chaos: dict[str, Any] = field(default_factory=dict)
    drain: dict[str, Any] = field(default_factory=dict)
    gpu_leases: int = 0
    gpu_misplacements: int = 0
    cpu_jobs_on_gpu: int = 0
    registered_event: asyncio.Event = field(default_factory=asyncio.Event)

    def wall_clock(self, perf: float) -> float:
        return self.started_wall + (perf - self.started)

    def report(self, elapsed: float) -> dict[str, object]:
        return {
            "registered_workers": self.registered,
            "registered_after_seconds": self.registered_after_seconds,
            "leases": self.leases,
            "completed": self.completed,
            "stream_errors": self.stream_errors,
            "elapsed_seconds": round(elapsed, 3),
            "completion_throughput_per_second": (
                round(self.completed / elapsed, 3) if elapsed else 0
            ),
            "lease_latency_ms": percentiles(self.lease_latencies_ms),
            "submission": self.submission,
            "chaos": self.chaos,
            "drain": self.drain,
            "mixed_fleet": {
                "gpu_leases": self.gpu_leases,
                "gpu_misplacements": self.gpu_misplacements,
                "cpu_jobs_on_gpu": self.cpu_jobs_on_gpu,
            },
        }


class SimulatedWorker:
    def __init__(
        self,
        number: int,
        address: str,
        measurements: Measurements,
        rng: random.Random,
        min_duration_ms: int,
        max_duration_ms: int,
        failure_rate: float,
        disappear_rate: float,
        gpu_count: int = 0,
        vram_mb: int = 0,
    ) -> None:
        self.worker_id = f"sim-{number:08d}"
        self.address = address
        self.measurements = measurements
        self.rng = rng
        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self.failure_rate = failure_rate
        self.disappear_rate = disappear_rate
        self.gpu_count = gpu_count
        self.vram_mb = vram_mb
        self.capabilities = ["network-disabled", "simulated"]
        if gpu_count:
            self.capabilities.append("cuda")
        self.heartbeat_seconds = 5.0
        self.active: set[str] = set()
        self.executions: set[asyncio.Task[None]] = set()
        self.killed = False
        self.task: asyncio.Task[None] | None = None
        self.outgoing: asyncio.Queue[worker_pb2.WorkerMessage] = asyncio.Queue(maxsize=100)

    async def run(self, stop: asyncio.Event) -> None:
        async with grpc.aio.insecure_channel(self.address) as channel:
            stub = worker_pb2_grpc.WorkerControlStub(channel)  # type: ignore[no-untyped-call]

            async def requests() -> AsyncIterator[worker_pb2.WorkerMessage]:
                yield worker_pb2.WorkerMessage(
                    worker_id=self.worker_id,
                    register=worker_pb2.Register(
                        protocol_version="v1",
                        worker_version="loadgen-0.2.0",
                        cpu_millis=8000,
                        memory_mb=16384,
                        pids=4096,
                        capabilities=self.capabilities,
                        sandbox_backends=["gvisor"],
                        gpu_count=self.gpu_count,
                        vram_mb=self.vram_mb,
                    ),
                )
                self.measurements.registered += 1
                if self.measurements.registered >= self.measurements.target_workers:
                    self.measurements.registered_after_seconds = round(
                        time.perf_counter() - self.measurements.started, 3
                    )
                    self.measurements.registered_event.set()
                # Heartbeat on a fixed cadence like the Go worker's ticker, regardless of
                # how busy the stream is. Heartbeating only when idle let a worker that
                # receives a steady stream of leases fall silent, so its running leases
                # expired mid-job (see benchmarks/reports).
                next_heartbeat = time.monotonic() + self.heartbeat_seconds
                while not stop.is_set():
                    timeout = max(0.0, next_heartbeat - time.monotonic())
                    try:
                        message = await asyncio.wait_for(self.outgoing.get(), timeout=timeout)
                    except TimeoutError:
                        message = worker_pb2.WorkerMessage(
                            worker_id=self.worker_id,
                            heartbeat=worker_pb2.Heartbeat(
                                unix_millis=int(time.time() * 1000),
                                active_attempt_ids=sorted(self.active),
                            ),
                        )
                        next_heartbeat = time.monotonic() + self.heartbeat_seconds
                    yield message

            try:
                async for control in stub.Connect(requests()):
                    kind = control.WhichOneof("payload")
                    if kind == "lease":
                        if self.rng.random() < self.disappear_rate:
                            return
                        task = asyncio.create_task(self.execute(control.lease))
                        self.executions.add(task)
                        task.add_done_callback(self.executions.discard)
                    elif kind == "cancel":
                        self.active.discard(control.cancel.attempt_id)
            except grpc.aio.AioRpcError:
                if not stop.is_set() and not self.killed:
                    self.measurements.stream_errors += 1

    def kill(self) -> list[str]:
        """Abruptly drop the stream: no completion, no cleanup, no further heartbeat."""
        self.killed = True
        in_flight = sorted(self.active)
        for task in list(self.executions):
            task.cancel()
        if self.task is not None:
            self.task.cancel()
        return in_flight

    async def execute(self, lease: worker_pb2.LeaseOffer) -> None:
        received = time.perf_counter()
        self.measurements.leases += 1
        if lease.gpu_count:
            self.measurements.gpu_leases += 1
            if self.gpu_count < lease.gpu_count or "cuda" not in self.capabilities:
                self.measurements.gpu_misplacements += 1
        elif self.gpu_count:
            self.measurements.cpu_jobs_on_gpu += 1
        self.active.add(lease.attempt_id)
        await self.outgoing.put(
            worker_pb2.WorkerMessage(
                worker_id=self.worker_id,
                acknowledgement=worker_pb2.LeaseAcknowledgement(
                    run_id=lease.run_id,
                    attempt_id=lease.attempt_id,
                    lease_token=lease.lease_token,
                    accepted=True,
                ),
            )
        )
        submitted = self.measurements.submitted_at.get(lease.run_id, received)
        self.measurements.lease_latencies_ms.append((received - submitted) * 1000)
        await asyncio.sleep(self.rng.randint(self.min_duration_ms, self.max_duration_ms) / 1000)
        failed = self.rng.random() < self.failure_rate
        await self.outgoing.put(
            worker_pb2.WorkerMessage(
                worker_id=self.worker_id,
                completion=worker_pb2.RunCompletion(
                    run_id=lease.run_id,
                    attempt_id=lease.attempt_id,
                    lease_token=lease.lease_token,
                    exit_code=1 if failed else 0,
                    terminal_state="FAILED" if failed else "SUCCEEDED",
                    reason_code="SIMULATED_FAILURE" if failed else "",
                    message="injected by load generator" if failed else "",
                ),
            )
        )
        self.active.discard(lease.attempt_id)
        self.measurements.completed += 1


async def benchmark(args: argparse.Namespace) -> dict[str, object]:
    stop = asyncio.Event()
    measurements = Measurements(target_workers=args.workers)
    prefix = uuid.uuid4().hex
    workers = [
        SimulatedWorker(
            number=args.worker_offset + index,
            address=args.control,
            measurements=measurements,
            rng=random.Random(args.seed + args.worker_offset + index),
            min_duration_ms=args.min_duration_ms,
            max_duration_ms=args.max_duration_ms,
            failure_rate=args.failure_rate,
            disappear_rate=args.disappear_rate,
            gpu_count=args.gpu_count_per_worker if index < args.gpu_workers else 0,
            vram_mb=args.gpu_vram_mb_per_worker if index < args.gpu_workers else 0,
        )
        for index in range(args.workers)
    ]
    for worker in workers:
        worker.task = asyncio.create_task(worker.run(stop))
    tasks = [worker.task for worker in workers if worker.task is not None]
    try:
        try:
            await asyncio.wait_for(
                measurements.registered_event.wait(), timeout=args.register_timeout
            )
        except TimeoutError:
            measurements.submission["registration_timeout"] = True
        await submit_jobs(args, measurements, prefix)
        chaos_task = asyncio.create_task(inject_chaos(args, workers, measurements))
        await wait_for_drain(args, measurements, prefix)
        chaos_task.cancel()
        await asyncio.gather(chaos_task, return_exceptions=True)
    finally:
        # Measurement is over: tear the fleet down without waiting for each stream's
        # next heartbeat tick, which took tens of minutes against a saturated gateway.
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - measurements.started
    configuration = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
        if key not in {"api_key", "output", "database_url"}
    }
    result: dict[str, object] = {
        "benchmark_id": prefix,
        "started_at": datetime.fromtimestamp(measurements.started_wall, UTC).isoformat(),
        "configuration": configuration,
        "results": measurements.report(elapsed),
    }
    if args.database_url:
        result["audit"] = await audit(args.database_url, prefix, measurements)
    return result


async def submit_jobs(args: argparse.Namespace, measurements: Measurements, prefix: str) -> None:
    submission: dict[str, Any] = {
        "attempted": args.jobs,
        "accepted": 0,
        "backpressure_429": 0,
        "errors": 0,
        "seconds": 0.0,
        "per_second": 0.0,
    }
    measurements.submission = submission
    if args.jobs == 0:
        return
    semaphore = asyncio.Semaphore(args.submit_concurrency)
    body: dict[str, Any] = {
        "repository": {"url": "https://github.com/octocat/Hello-World", "ref": "HEAD"},
        "argv": ["true"],
        "profile": "python",
        "network": "disabled",
        "resources": {
            "cpu_millis": 100,
            "memory_mb": 128,
            "pids": 16,
            "disk_mb": 128,
            "timeout_seconds": 30,
        },
        "retry": {"safe_on_worker_loss": True, "max_attempts": 3},
    }
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as client:

        async def submit(number: int) -> None:
            async with semaphore:
                backoff = 0.05
                while True:
                    try:
                        response = await client.post(
                            f"{args.api.rstrip('/')}/runs",
                            headers={
                                "Authorization": f"Bearer {args.api_key}",
                                "Idempotency-Key": f"loadgen-{prefix}-{number}",
                            },
                            json=(
                                {
                                    **body,
                                    "profile": "cuda",
                                    "required_capabilities": ["cuda"],
                                    "resources": {
                                        **body["resources"],
                                        "gpu": 1,
                                        "vram_mb": args.gpu_job_vram_mb,
                                    },
                                }
                                if number < round(args.jobs * args.gpu_job_fraction)
                                else body
                            ),
                        )
                    except httpx.HTTPError:
                        submission["errors"] += 1
                        return
                    if response.status_code == 429:
                        # Admission control is the designed backpressure signal: honour it.
                        submission["backpressure_429"] += 1
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 2.0)
                        continue
                    if response.status_code >= 400:
                        submission["errors"] += 1
                        return
                    run_id = response.json()["id"]
                    measurements.submitted_at[run_id] = time.perf_counter()
                    submission["accepted"] += 1
                    return

        await asyncio.gather(*(submit(number) for number in range(args.jobs)))
    submission["seconds"] = round(time.perf_counter() - started, 3)
    submission["per_second"] = round(submission["accepted"] / submission["seconds"], 3)


async def inject_chaos(
    args: argparse.Namespace, workers: list[SimulatedWorker], measurements: Measurements
) -> None:
    if args.kill_fraction <= 0:
        return
    await asyncio.sleep(args.kill_after_seconds)
    rng = random.Random(args.seed)
    count = max(1, round(len(workers) * args.kill_fraction))
    if args.kill_selection == "busiest":
        # Worst case: take the workers carrying the most in-flight attempts first,
        # then fill the fraction with a seeded random sample of the rest.
        busy = sorted(
            (worker for worker in workers if worker.active),
            key=lambda worker: (-len(worker.active), worker.worker_id),
        )[:count]
        idle = [worker for worker in workers if worker not in busy]
        victims = busy + rng.sample(idle, count - len(busy))
    else:
        victims = rng.sample(workers, count)
    killed_perf = time.perf_counter()
    in_flight: dict[str, list[str]] = {}
    for worker in victims:
        in_flight[worker.worker_id] = worker.kill()
    measurements.chaos = {
        "kind": "worker-loss",
        "selection": args.kill_selection,
        "killed_workers": count,
        "killed_fraction": round(count / len(workers), 4),
        "killed_at": datetime.fromtimestamp(measurements.wall_clock(killed_perf), UTC).isoformat(),
        "killed_after_start_seconds": round(killed_perf - measurements.started, 3),
        "in_flight_attempts_at_kill": sum(len(ids) for ids in in_flight.values()),
        "killed_worker_ids": sorted(in_flight),
    }


async def wait_for_drain(args: argparse.Namespace, measurements: Measurements, prefix: str) -> None:
    """Wait until every submitted run is terminal or the duration budget is spent."""
    deadline = time.perf_counter() + args.duration
    if not args.database_url:
        await asyncio.sleep(args.duration)
        return
    connection = await asyncpg.connect(_dsn(args.database_url))
    try:
        while True:
            remaining = await connection.fetchval(
                "SELECT count(*) FROM runs WHERE idempotency_key LIKE $1"
                " AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','LOST')",
                f"loadgen-{prefix}-%",
            )
            now = time.perf_counter()
            if remaining == 0 and measurements.submitted_at:
                measurements.drain = {
                    "drained": True,
                    "seconds_after_start": round(now - measurements.started, 3),
                }
                return
            if now >= deadline:
                measurements.drain = {
                    "drained": False,
                    "seconds_after_start": round(now - measurements.started, 3),
                    "non_terminal_runs": remaining,
                }
                return
            await asyncio.sleep(min(2.0, max(0.1, deadline - now)))
    finally:
        await connection.close()


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def audit(database_url: str, prefix: str, measurements: Measurements) -> dict[str, Any]:
    """Read the authoritative outcome of this benchmark's runs from PostgreSQL."""
    like = f"loadgen-{prefix}-%"
    connection = await asyncpg.connect(_dsn(database_url))
    try:
        states = {
            row["state"]: row["count"]
            for row in await connection.fetch(
                "SELECT state, count(*) AS count FROM runs WHERE idempotency_key LIKE $1"
                " GROUP BY state",
                like,
            )
        }
        latency_rows = await connection.fetch(
            "SELECT EXTRACT(EPOCH FROM (started_at - created_at)) * 1000 AS to_start_ms,"
            " EXTRACT(EPOCH FROM (finished_at - created_at)) * 1000 AS end_to_end_ms,"
            " attempt_count, coalesce((spec->'resources'->>'gpu')::int,0) AS gpu"
            " FROM runs WHERE idempotency_key LIKE $1",
            like,
        )
        attempt_states = {
            row["state"]: row["count"]
            for row in await connection.fetch(
                "SELECT a.state, count(*) AS count FROM attempts a JOIN runs r ON r.id = a.run_id"
                " WHERE r.idempotency_key LIKE $1 GROUP BY a.state",
                like,
            )
        }
        reservations = await connection.fetchrow(
            "SELECT coalesce(sum(reserved_cpu_millis),0) AS cpu,"
            " coalesce(sum(reserved_memory_mb),0) AS memory,"
            " coalesce(sum(reserved_pids),0) AS pids,"
            " coalesce(sum(reserved_gpu_count),0) AS gpu,"
            " coalesce(sum(reserved_vram_mb),0) AS vram, count(*) AS workers FROM workers"
        )
        unpublished = await connection.fetchval(
            "SELECT count(*) FROM outbox_events WHERE published_at IS NULL"
        )
        in_flight = await connection.fetchrow(
            "SELECT coalesce(sum((spec->'resources'->>'cpu_millis')::bigint),0) AS cpu,"
            " coalesce(sum((spec->'resources'->>'memory_mb')::bigint),0) AS memory,"
            " coalesce(sum((spec->'resources'->>'pids')::bigint),0) AS pids,"
            " coalesce(sum(coalesce(spec->'resources'->>'gpu','0')::bigint),0) AS gpu,"
            " coalesce(sum(coalesce(spec->'resources'->>'vram_mb','0')::bigint),0) AS vram"
            " FROM runs WHERE idempotency_key LIKE $1"
            " AND state IN ('LEASED','RUNNING','CANCEL_REQUESTED')",
            like,
        )
        result: dict[str, Any] = {
            "run_states": states,
            "attempt_states": attempt_states,
            "runs_total": sum(states.values()),
            "runs_lost": states.get("LOST", 0),
            "runs_non_terminal": sum(
                count for state, count in states.items() if state not in TERMINAL
            ),
            "runs_with_retries": sum(1 for row in latency_rows if row["attempt_count"] > 1),
            "time_to_start_ms": percentiles(
                [
                    float(row["to_start_ms"])
                    for row in latency_rows
                    if row["to_start_ms"] is not None
                ]
            ),
            "time_to_start_by_class_ms": {
                class_name: percentiles(
                    [
                        float(row["to_start_ms"])
                        for row in latency_rows
                        if row["to_start_ms"] is not None
                        and (row["gpu"] > 0) == (class_name == "gpu")
                    ]
                )
                for class_name in ("cpu", "gpu")
            },
            "end_to_end_ms": percentiles(
                [
                    float(row["end_to_end_ms"])
                    for row in latency_rows
                    if row["end_to_end_ms"] is not None
                ]
            ),
            "workers_in_table": reservations["workers"] if reservations else 0,
            # Reservations still held minus what the runs still in flight legitimately
            # hold. Anything left is a resource-accounting bug.
            "reserved_after_run": {
                "cpu_millis": int(reservations["cpu"]) if reservations else 0,
                "memory_mb": int(reservations["memory"]) if reservations else 0,
                "pids": int(reservations["pids"]) if reservations else 0,
                "gpu": int(reservations["gpu"]) if reservations else 0,
                "vram_mb": int(reservations["vram"]) if reservations else 0,
            },
            "in_flight_reservations": {
                "cpu_millis": int(in_flight["cpu"]) if in_flight else 0,
                "memory_mb": int(in_flight["memory"]) if in_flight else 0,
                "pids": int(in_flight["pids"]) if in_flight else 0,
                "gpu": int(in_flight["gpu"]) if in_flight else 0,
                "vram_mb": int(in_flight["vram"]) if in_flight else 0,
            },
            "leaked_reservations": {
                "cpu_millis": int(reservations["cpu"] - in_flight["cpu"])
                if reservations and in_flight
                else 0,
                "memory_mb": int(reservations["memory"] - in_flight["memory"])
                if reservations and in_flight
                else 0,
                "pids": int(reservations["pids"] - in_flight["pids"])
                if reservations and in_flight
                else 0,
                "gpu": int(reservations["gpu"] - in_flight["gpu"])
                if reservations and in_flight
                else 0,
                "vram_mb": int(reservations["vram"] - in_flight["vram"])
                if reservations and in_flight
                else 0,
            },
            "unpublished_outbox_events": unpublished,
        }
        if measurements.chaos:
            result["recovery"] = await audit_recovery(connection, like, measurements.chaos)
        return result
    finally:
        await connection.close()


async def audit_recovery(
    connection: asyncpg.Connection, like: str, chaos: dict[str, Any]
) -> dict[str, Any]:
    killed_at = datetime.fromisoformat(chaos["killed_at"])
    victims = list(chaos["killed_worker_ids"])
    rows = await connection.fetch(
        "SELECT a.id AS attempt_id, a.state AS attempt_state, a.acknowledged_at, a.finished_at,"
        " a.lease_expires_at, r.id AS run_id, r.state AS run_state, r.attempt_count,"
        " r.finished_at AS run_finished_at"
        " FROM attempts a JOIN runs r ON r.id = a.run_id"
        " WHERE r.idempotency_key LIKE $1 AND a.worker_id = ANY($2::text[])"
        " AND a.state = 'LOST'",
        like,
        victims,
    )
    detection = [
        (row["finished_at"] - killed_at).total_seconds() for row in rows if row["finished_at"]
    ]
    recovery = [
        (row["run_finished_at"] - killed_at).total_seconds()
        for row in rows
        if row["run_finished_at"] is not None
    ]
    affected_runs = {row["run_id"] for row in rows}
    outcomes: dict[str, int] = {}
    for row in rows:
        outcomes[row["run_state"]] = outcomes.get(row["run_state"], 0) + 1
    later_success = sum(
        1
        for row in rows
        if row["run_state"] in ("SUCCEEDED", "FAILED") and row["attempt_count"] > 1
    )
    return {
        "killed_at": chaos["killed_at"],
        "lost_attempts_on_killed_workers": len(rows),
        "affected_runs": len(affected_runs),
        "affected_run_outcomes": outcomes,
        "requeued_and_finished_elsewhere": later_success,
        "runs_lost": outcomes.get("LOST", 0),
        "detection_seconds": percentiles(detection),
        "recovery_seconds": percentiles(recovery),
        "acknowledged_before_kill": sum(1 for row in rows if row["acknowledged_at"] is not None),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Agent Fabric simulated worker fleet")
    result.add_argument("--control", default="localhost:50051")
    result.add_argument("--api", default="http://localhost:8000")
    result.add_argument("--api-key", default="af_dev_key")
    result.add_argument("--workers", type=int, default=100)
    result.add_argument("--worker-offset", type=int, default=0)
    result.add_argument("--gpu-workers", type=int, default=0)
    result.add_argument("--gpu-count-per-worker", type=int, default=1)
    result.add_argument("--gpu-vram-mb-per-worker", type=int, default=16384)
    result.add_argument("--gpu-job-fraction", type=float, default=0.0)
    result.add_argument("--gpu-job-vram-mb", type=int, default=8192)
    result.add_argument("--jobs", type=int, default=1000)
    result.add_argument("--duration", type=int, default=60, help="maximum seconds to wait")
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--min-duration-ms", type=int, default=50)
    result.add_argument("--max-duration-ms", type=int, default=500)
    result.add_argument("--failure-rate", type=float, default=0.0)
    result.add_argument("--disappear-rate", type=float, default=0.0)
    result.add_argument("--register-timeout", type=float, default=30.0)
    result.add_argument("--submit-concurrency", type=int, default=100)
    result.add_argument("--kill-fraction", type=float, default=0.0)
    result.add_argument("--kill-after-seconds", type=float, default=5.0)
    result.add_argument("--kill-selection", choices=["random", "busiest"], default="random")
    result.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="enable the PostgreSQL audit (defaults to DATABASE_URL)",
    )
    result.add_argument("--label", default="")
    result.add_argument("--output", type=Path)
    return result


def run() -> None:
    args = parser().parse_args()
    if args.workers < 1 or args.workers > 1_000_000:
        raise SystemExit("--workers must be between 1 and 1,000,000")
    if args.jobs < 0:
        raise SystemExit("--jobs cannot be negative")
    if not 0 <= args.gpu_workers <= args.workers:
        raise SystemExit("--gpu-workers must be between zero and --workers")
    if not 0 <= args.gpu_job_fraction <= 1:
        raise SystemExit("--gpu-job-fraction must be between zero and one")
    if not 0 <= args.failure_rate <= 1 or not 0 <= args.disappear_rate <= 1:
        raise SystemExit("failure rates must be between zero and one")
    if not 0 <= args.kill_fraction <= 1:
        raise SystemExit("--kill-fraction must be between zero and one")
    result = asyncio.run(benchmark(args))
    rendered = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
