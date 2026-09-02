"""Chaos scenarios with measured detection, recovery, and job-loss numbers.

Each scenario runs a simulated fleet against a live control plane, submits retry-safe
jobs, injects one fault while work is in flight, and then reads PostgreSQL (the
authoritative store) to compute:

- detection_seconds: fault time -> first attempt marked LOST (worker-loss scenarios)
- requeued_runs: runs that lost an attempt and were leased again
- recovery_seconds: fault time -> last affected run reaching a terminal state
- placement_gap_seconds: longest pause between consecutive lease placements after the fault
- lost_runs: runs that ended in the LOST state (the job-loss count)
- unfinished_runs: runs not terminal by the deadline (also job loss, reported separately)

Usage: python tests/chaos/run_scenarios.py --scenario all --output benchmarks/results/chaos
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from agent_fabric import loadgen

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))
from run_native import environment_facts  # noqa: E402

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "LOST")


async def sql(pool: asyncpg.Pool, query: str, *params: Any) -> list[asyncpg.Record]:
    async with pool.acquire() as connection:
        return await connection.fetch(query, *params)


async def db_now(pool: asyncpg.Pool) -> float:
    rows = await sql(pool, "SELECT extract(epoch FROM clock_timestamp())::float8 AS t")
    return float(rows[0]["t"])


def shell(command: str) -> None:
    print(f"  $ {command}", flush=True)
    subprocess.run(command, shell=True, check=True, timeout=120)


class Scenario:
    def __init__(self, name: str, args: argparse.Namespace) -> None:
        self.name = name
        self.args = args
        self.stop = asyncio.Event()
        self.measurements = loadgen.Measurements(target_workers=args.workers)
        self.workers: list[loadgen.SimulatedWorker] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.killed: list[str] = []
        self.fault_at: float | None = None
        self.notes: list[str] = []
        self.prefix = uuid.uuid4().hex

    async def start_fleet(self) -> None:
        args = self.args
        self.workers = [
            loadgen.SimulatedWorker(
                number=index,
                address=args.control,
                measurements=self.measurements,
                rng=random.Random(args.seed + index),
                min_duration_ms=args.min_duration_ms,
                max_duration_ms=args.max_duration_ms,
                failure_rate=args.failure_rate,
                disappear_rate=args.disappear_rate,
            )
            for index in range(args.workers)
        ]
        for worker in self.workers:
            worker.task = asyncio.create_task(worker.run(self.stop))
        self.tasks = [worker.task for worker in self.workers if worker.task is not None]
        await asyncio.wait_for(self.measurements.registered_event.wait(), timeout=120)

    async def submit(self) -> None:
        await loadgen.submit_jobs(self.args, self.measurements, self.prefix)

    async def inject(self, pool: asyncpg.Pool) -> None:
        rng = random.Random(self.args.seed)
        if self.name in {"kill-worker", "kill-fleet-10pct"}:
            busy = [worker for worker in self.workers if worker.active]
            count = 1 if self.name == "kill-worker" else max(1, len(self.workers) // 10)
            victims = rng.sample(busy, min(count, len(busy)))
            if len(victims) < count:
                idle = [worker for worker in self.workers if worker not in victims]
                victims += rng.sample(idle, count - len(victims))
            self.fault_at = await db_now(pool)
            for worker in victims:
                worker.kill()
                self.killed.append(worker.worker_id)
            self.notes.append(
                f"killed {len(victims)} worker(s) holding "
                f"{sum(len(w.active) for w in victims)} active attempt(s)"
            )
        elif self.name == "scheduler-restart":
            self.fault_at = await db_now(pool)
            await asyncio.to_thread(shell, self.args.scheduler_restart_cmd)
        elif self.name == "postgres-restart":
            self.fault_at = await db_now(pool)
            await asyncio.to_thread(shell, self.args.postgres_restart_cmd)
        elif self.name == "redis-restart":
            self.fault_at = await db_now(pool)
            await asyncio.to_thread(shell, self.args.redis_restart_cmd)
        else:
            raise SystemExit(f"unknown scenario {self.name}")

    async def wait_for_drain(self, pool: asyncpg.Pool, run_ids: list[str]) -> bool:
        deadline = time.monotonic() + self.args.deadline
        while time.monotonic() < deadline:
            rows = await sql(
                pool,
                "SELECT count(*) AS n FROM runs WHERE id = ANY($1::uuid[]) "
                "AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','LOST')",
                run_ids,
            )
            if rows[0]["n"] == 0:
                return True
            await asyncio.sleep(0.5)
        return False

    async def analyse(self, pool: asyncpg.Pool, run_ids: list[str], drained: bool) -> dict:
        fault = self.fault_at or 0.0
        states = {
            row["state"]: row["n"]
            for row in await sql(
                pool,
                "SELECT state, count(*) AS n FROM runs WHERE id = ANY($1::uuid[]) GROUP BY state",
                run_ids,
            )
        }
        attempt_states = {
            row["state"]: row["n"]
            for row in await sql(
                pool,
                "SELECT state, count(*) AS n FROM attempts WHERE run_id = ANY($1::uuid[]) "
                "GROUP BY state",
                run_ids,
            )
        }
        all_lost = await sql(
            pool,
            "SELECT run_id, worker_id, acknowledged_at IS NOT NULL AS acknowledged, "
            "extract(epoch FROM finished_at)::float8 AS finished "
            "FROM attempts WHERE run_id = ANY($1::uuid[]) AND state = 'LOST' ORDER BY finished_at",
            run_ids,
        )
        # For worker-loss scenarios only leases held by the killed workers count as
        # affected; offers that expired elsewhere (e.g. delivery backlog) are reported apart.
        if self.killed:
            killed = set(self.killed)
            lost_attempts = [row for row in all_lost if row["worker_id"] in killed]
        else:
            lost_attempts = list(all_lost)
        other_lost = len(all_lost) - len(lost_attempts)
        affected = sorted({str(row["run_id"]) for row in lost_attempts})
        detection = None
        if lost_attempts and self.fault_at:
            detection = round(float(lost_attempts[0]["finished"]) - fault, 3)
        requeued = 0
        recovery = None
        if affected:
            rows = await sql(
                pool,
                "SELECT r.id, r.state, r.attempt_count, "
                "extract(epoch FROM r.finished_at)::float8 AS finished "
                "FROM runs r WHERE r.id = ANY($1::uuid[])",
                affected,
            )
            requeued = sum(1 for row in rows if row["attempt_count"] > 1)
            finished = [row["finished"] for row in rows if row["finished"] is not None]
            if finished and len(finished) == len(rows):
                recovery = round(max(finished) - fault, 3)
        placements = await sql(
            pool,
            "SELECT extract(epoch FROM a.lease_expires_at)::float8 - $2 AS t FROM attempts a "
            "WHERE a.run_id = ANY($1::uuid[]) ORDER BY a.lease_expires_at",
            run_ids,
            float(self.args.acknowledgement_deadline_seconds),
        )
        gap = 0.0
        gap_at = None
        previous = None
        for row in placements:
            t = float(row["t"])
            if previous is not None and t - previous > gap and t >= fault - 1:
                gap = t - previous
                gap_at = round(previous - fault, 3)
            previous = t
        drain_rows = await sql(
            pool,
            "SELECT extract(epoch FROM max(finished_at) - min(created_at))::float8 AS drain, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - "
            "created_at))::float8 AS p50, percentile_cont(0.99) WITHIN GROUP (ORDER BY "
            "extract(epoch FROM finished_at - created_at))::float8 AS p99 "
            "FROM runs WHERE id = ANY($1::uuid[]) AND finished_at IS NOT NULL",
            run_ids,
        )
        total_drain = (
            round(float(drain_rows[0]["drain"]), 3) if drain_rows[0]["drain"] is not None else None
        )
        e2e = {
            "p50_s": round(float(drain_rows[0]["p50"]), 3) if drain_rows[0]["p50"] else None,
            "p99_s": round(float(drain_rows[0]["p99"]), 3) if drain_rows[0]["p99"] else None,
        }
        return {
            "scenario": self.name,
            "fault_injected": self.fault_at is not None,
            "killed_workers": len(self.killed),
            "notes": self.notes,
            "jobs": len(run_ids),
            "drained_before_deadline": drained,
            "runs_by_state": states,
            "attempts_by_state": attempt_states,
            "lost_attempts": len(lost_attempts),
            "other_expired_leases": other_lost,
            "lost_attempts_acknowledged": sum(1 for row in lost_attempts if row["acknowledged"]),
            "affected_runs": len(affected),
            "requeued_runs": requeued,
            "detection_seconds": detection,
            "recovery_seconds": recovery,
            "placement_gap_seconds": round(gap, 3),
            "placement_gap_started_after_fault_seconds": gap_at,
            "lost_runs": states.get("LOST", 0),
            "unfinished_runs": sum(n for s, n in states.items() if s not in TERMINAL),
            "total_drain_seconds": total_drain,
            "end_to_end_seconds": e2e,
        }

    async def run(self, pool: asyncpg.Pool) -> dict:
        print(f"== scenario {self.name}", flush=True)
        try:
            await self.start_fleet()
            submitter = asyncio.create_task(self.submit())
            await asyncio.sleep(self.args.fault_after)
            # Do not inject into an idle fleet: wait until leases are actually in flight.
            waited = 0.0
            while (
                sum(len(worker.active) for worker in self.workers) < self.args.min_in_flight
                and waited < self.args.in_flight_timeout
            ):
                await asyncio.sleep(0.25)
                waited += 0.25
            self.notes.append(
                f"fault injected {self.args.fault_after + waited:.2f}s after submission started "
                f"with {sum(len(w.active) for w in self.workers)} attempt(s) in flight"
            )
            await self.inject(pool)
            await submitter
            run_ids = list(self.measurements.submitted_at)
            drained = await self.wait_for_drain(pool, run_ids)
            result = await self.analyse(pool, run_ids, drained)
        finally:
            self.stop.set()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        print(
            json.dumps(
                {
                    k: result[k]
                    for k in (
                        "detection_seconds",
                        "recovery_seconds",
                        "requeued_runs",
                        "lost_runs",
                        "unfinished_runs",
                        "placement_gap_seconds",
                    )
                }
            ),
            flush=True,
        )
        return result


ALL = ["kill-worker", "kill-fleet-10pct", "scheduler-restart", "postgres-restart", "redis-restart"]


def parser() -> argparse.ArgumentParser:
    result = loadgen.parser()
    result.description = "Agent Fabric chaos scenarios"
    result.set_defaults(
        workers=200,
        jobs=2000,
        min_duration_ms=500,
        max_duration_ms=2000,
        duration=600,
    )
    result.add_argument("--scenario", default="all", help="one of all, " + ", ".join(ALL))
    result.add_argument(
        "--fault-after",
        type=float,
        default=3.0,
        help="seconds after submission starts before injecting the fault",
    )
    result.add_argument(
        "--min-in-flight",
        type=int,
        default=20,
        help="wait for at least this many active leases before injecting the fault",
    )
    result.add_argument("--in-flight-timeout", type=float, default=60.0)
    result.add_argument(
        "--deadline",
        type=float,
        default=300.0,
        help="seconds to wait for all runs to reach a terminal state",
    )
    result.add_argument("--acknowledgement-deadline-seconds", type=int, default=10)
    result.add_argument(
        "--scheduler-restart-cmd", default=os.environ.get("AF_SCHEDULER_RESTART", "")
    )
    result.add_argument("--postgres-restart-cmd", default=os.environ.get("AF_POSTGRES_RESTART", ""))
    result.add_argument("--redis-restart-cmd", default=os.environ.get("AF_REDIS_RESTART", ""))
    result.add_argument(
        "--reset-cmd",
        default=os.environ.get("AF_RESET", ""),
        help="shell command run before each scenario (e.g. truncate tables)",
    )
    return result


async def main() -> int:
    args = parser().parse_args()
    args.output = Path(args.output) if args.output else Path("benchmarks/results/chaos")
    args.output.mkdir(parents=True, exist_ok=True)
    scenarios = ALL if args.scenario == "all" else [args.scenario]
    dsn = (args.database_url or os.environ.get("DATABASE_URL", "")).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    ) or "postgresql://agent_fabric:agent_fabric@127.0.0.1:5432/agent_fabric"
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    results = []
    try:
        for name in scenarios:
            needed = {
                "scheduler-restart": args.scheduler_restart_cmd,
                "postgres-restart": args.postgres_restart_cmd,
                "redis-restart": args.redis_restart_cmd,
            }.get(name, "ok")
            if not needed:
                print(f"== scenario {name}: skipped, no restart command configured")
                continue
            if args.reset_cmd:
                await asyncio.to_thread(shell, args.reset_cmd)
            result = await Scenario(name, args).run(pool)
            result["configuration"] = {
                k: v
                for k, v in vars(args).items()
                if k not in {"api_key", "output", "database_url"} and not k.endswith("_cmd")
            }
            result["environment"] = environment_facts()
            (args.output / f"{name}.json").write_text(json.dumps(result, indent=2, sort_keys=True))
            results.append(result)
            await asyncio.sleep(2)
    finally:
        await pool.close()
    # The summary covers every scenario file in the directory, not only this invocation.
    combined = []
    for name in ALL:
        file = args.output / f"{name}.json"
        if file.exists():
            combined.append(json.loads(file.read_text()))
    (args.output / "summary.json").write_text(json.dumps(combined, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
