import argparse
import asyncio
import json
import random
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import grpc
import httpx

from .generated import worker_pb2, worker_pb2_grpc


@dataclass
class Measurements:
    target_workers: int
    registered: int = 0
    leases: int = 0
    completed: int = 0
    failed: int = 0
    lease_latencies_ms: list[float] = field(default_factory=list)
    submitted_at: dict[str, float] = field(default_factory=dict)
    registered_event: asyncio.Event = field(default_factory=asyncio.Event)

    def report(self, elapsed: float) -> dict[str, object]:
        ordered = sorted(self.lease_latencies_ms)

        def percentile(value: float) -> float | None:
            if not ordered:
                return None
            index = min(len(ordered) - 1, round((len(ordered) - 1) * value))
            return round(ordered[index], 3)

        return {
            "registered_workers": self.registered,
            "leases": self.leases,
            "completed": self.completed,
            "failed": self.failed,
            "elapsed_seconds": round(elapsed, 3),
            "completion_throughput_per_second": (
                round(self.completed / elapsed, 3) if elapsed else 0
            ),
            "lease_latency_ms": {
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "p99": percentile(0.99),
                "mean": round(statistics.mean(ordered), 3) if ordered else None,
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
    ) -> None:
        self.worker_id = f"sim-{number:08d}"
        self.address = address
        self.measurements = measurements
        self.rng = rng
        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self.failure_rate = failure_rate
        self.disappear_rate = disappear_rate
        self.active: set[str] = set()
        self.outgoing: asyncio.Queue[worker_pb2.WorkerMessage] = asyncio.Queue(maxsize=100)

    async def run(self, stop: asyncio.Event) -> None:
        async with grpc.aio.insecure_channel(self.address) as channel:
            stub = worker_pb2_grpc.WorkerControlStub(channel)  # type: ignore[no-untyped-call]

            async def requests() -> AsyncIterator[worker_pb2.WorkerMessage]:
                yield worker_pb2.WorkerMessage(
                    worker_id=self.worker_id,
                    register=worker_pb2.Register(
                        protocol_version="v1",
                        worker_version="loadgen-0.1.0",
                        cpu_millis=8000,
                        memory_mb=16384,
                        pids=4096,
                        capabilities=["network-disabled", "simulated"],
                        sandbox_backends=["gvisor"],
                    ),
                )
                self.measurements.registered += 1
                if self.measurements.registered >= self.measurements.target_workers:
                    self.measurements.registered_event.set()
                while not stop.is_set():
                    try:
                        message = await asyncio.wait_for(self.outgoing.get(), timeout=5)
                    except TimeoutError:
                        message = worker_pb2.WorkerMessage(
                            worker_id=self.worker_id,
                            heartbeat=worker_pb2.Heartbeat(
                                unix_millis=int(time.time() * 1000),
                                active_attempt_ids=sorted(self.active),
                            ),
                        )
                    yield message

            try:
                async for control in stub.Connect(requests()):
                    kind = control.WhichOneof("payload")
                    if kind == "lease":
                        if self.rng.random() < self.disappear_rate:
                            return
                        asyncio.create_task(self.execute(control.lease))
                    elif kind == "cancel":
                        self.active.discard(control.cancel.attempt_id)
            except grpc.aio.AioRpcError:
                if not stop.is_set():
                    self.measurements.failed += 1

    async def execute(self, lease: worker_pb2.LeaseOffer) -> None:
        received = time.perf_counter()
        self.measurements.leases += 1
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
    started = time.perf_counter()
    workers = [
        SimulatedWorker(
            number=index,
            address=args.control,
            measurements=measurements,
            rng=random.Random(args.seed + index),
            min_duration_ms=args.min_duration_ms,
            max_duration_ms=args.max_duration_ms,
            failure_rate=args.failure_rate,
            disappear_rate=args.disappear_rate,
        )
        for index in range(args.workers)
    ]
    tasks = [asyncio.create_task(worker.run(stop)) for worker in workers]
    try:
        await asyncio.wait_for(measurements.registered_event.wait(), timeout=30)
        await submit_jobs(args, measurements)
        await asyncio.sleep(args.duration)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    configuration = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
        if key not in {"api_key", "output"}
    }
    result: dict[str, object] = {
        "benchmark_id": str(uuid.uuid4()),
        "configuration": configuration,
        "results": measurements.report(time.perf_counter() - started),
    }
    return result


async def submit_jobs(args: argparse.Namespace, measurements: Measurements) -> None:
    if args.jobs == 0:
        return
    semaphore = asyncio.Semaphore(100)
    body = {
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
    benchmark_prefix = uuid.uuid4().hex
    async with httpx.AsyncClient(timeout=30) as client:

        async def submit(number: int) -> None:
            async with semaphore:
                response = await client.post(
                    f"{args.api.rstrip('/')}/runs",
                    headers={
                        "Authorization": f"Bearer {args.api_key}",
                        "Idempotency-Key": f"loadgen-{benchmark_prefix}-{number}",
                    },
                    json=body,
                )
                response.raise_for_status()
                run_id = response.json()["id"]
                measurements.submitted_at[run_id] = time.perf_counter()

        await asyncio.gather(*(submit(number) for number in range(args.jobs)))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Agent Fabric simulated worker fleet")
    result.add_argument("--control", default="localhost:50051")
    result.add_argument("--api", default="http://localhost:8000")
    result.add_argument("--api-key", default="af_dev_key")
    result.add_argument("--workers", type=int, default=100)
    result.add_argument("--jobs", type=int, default=1000)
    result.add_argument("--duration", type=int, default=60)
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--min-duration-ms", type=int, default=50)
    result.add_argument("--max-duration-ms", type=int, default=500)
    result.add_argument("--failure-rate", type=float, default=0.0)
    result.add_argument("--disappear-rate", type=float, default=0.0)
    result.add_argument("--output", type=Path)
    return result


def run() -> None:
    args = parser().parse_args()
    if args.workers < 1 or args.workers > 1_000_000:
        raise SystemExit("--workers must be between 1 and 1,000,000")
    if args.jobs < 0:
        raise SystemExit("--jobs cannot be negative")
    if not 0 <= args.failure_rate <= 1 or not 0 <= args.disappear_rate <= 1:
        raise SystemExit("failure rates must be between zero and one")
    result = asyncio.run(benchmark(args))
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
