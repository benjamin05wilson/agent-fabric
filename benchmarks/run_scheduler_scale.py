"""Benchmark 1/2/4/8 scheduler processes against one persistent worker fleet."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_docker_scale import (
    ROOT,
    command,
    counter_rate,
    durable_worker_count,
    parse_loadgen,
    peaks,
    prometheus,
    sample,
)

SCHEDULERS = ["scheduler", *(f"scheduler-{index}" for index in range(1, 8))]


def reset_workload(limit: int) -> None:
    sql = (
        "TRUNCATE TABLE run_event_indexes, attempts, runs, outbox_events "
        "RESTART IDENTITY CASCADE; "
        f"UPDATE projects SET max_queued={limit},max_running={limit};"
    )
    command(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "agent_fabric",
            "-d",
            "agent_fabric",
            "-c",
            sql,
        ]
    )


def configure_schedulers(replicas: int) -> None:
    stop_schedulers()
    start_schedulers(replicas)


def stop_schedulers() -> None:
    command(["docker", "compose", "--profile", "scheduler-scale", "stop", *SCHEDULERS])


def start_schedulers(replicas: int) -> None:
    command(["docker", "compose", "up", "-d", *SCHEDULERS[:replicas]])


def durable_run_count() -> int:
    return int(
        command(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "agent_fabric",
                "-d",
                "agent_fabric",
                "-Atc",
                "SELECT count(*) FROM runs",
            ]
        )
    )


def attempt_delivery_audit() -> tuple[int, int]:
    output = command(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "agent_fabric",
            "-d",
            "agent_fabric",
            "-Atc",
            "SELECT (SELECT count(*) FROM (SELECT run_id FROM attempts "
            "WHERE acknowledged_at IS NOT NULL GROUP BY run_id HAVING count(*) > 1) d),"
            "(SELECT count(*) FROM attempts WHERE state='LOST' AND acknowledged_at IS NULL)",
        ]
    )
    duplicate_executions, expired_unacknowledged = output.split("|")
    return int(duplicate_executions), int(expired_unacknowledged)


def loadgen_command(name: str, arguments: list[str], *, detached: bool) -> list[str]:
    result = ["docker", "compose", "--profile", "load", "run"]
    if detached:
        result.append("-d")
    result += ["--rm", "--name", name, "loadgen", *arguments]
    return result


def start_fleet(args: argparse.Namespace) -> list[str]:
    per_shard = math.ceil(args.workers / args.fleet_shards)
    names: list[str] = []
    for shard in range(args.fleet_shards):
        offset = shard * per_shard
        count = min(per_shard, args.workers - offset)
        if count <= 0:
            break
        name = f"af-scheduler-fleet-{shard}"
        command(
            loadgen_command(
                name,
                [
                    "--control",
                    "grpc:50051",
                    "--api",
                    "http://api:8000",
                    "--workers",
                    str(count),
                    "--worker-offset",
                    str(offset),
                    "--expected-workers",
                    str(args.workers),
                    "--jobs",
                    "0",
                    "--duration",
                    str(args.fleet_duration),
                    "--register-timeout",
                    str(args.register_timeout),
                    "--label",
                    name,
                    "--output",
                    f"/results/{name}.json",
                ],
                detached=True,
            )
        )
        names.append(name)
    deadline = time.monotonic() + args.register_timeout
    while time.monotonic() < deadline:
        durable = durable_worker_count()
        active = prometheus('sum(agent_fabric_active_worker_streams{job="gateways"})') or 0
        if durable >= args.workers and active >= args.workers:
            return names
        time.sleep(2)
    raise RuntimeError(
        f"fleet registration timed out: durable={durable_worker_count()} active={active}"
    )


def run_workload(args: argparse.Namespace, replicas: int) -> dict[str, Any]:
    stop_schedulers()
    reset_workload(max(args.jobs * 2, 100000))
    name = f"af-scheduler-jobs-{replicas}"
    load_command = loadgen_command(
        name,
        [
            "--control",
            "grpc:50051",
            "--api",
            "http://api:8000",
            "--workers",
            "0",
            "--expected-workers",
            str(args.workers),
            "--jobs",
            str(args.jobs),
            "--duration",
            str(args.duration),
            "--register-timeout",
            str(args.register_timeout),
            "--min-duration-ms",
            str(args.min_duration_ms),
            "--max-duration-ms",
            str(args.max_duration_ms),
            "--label",
            f"scheduler-{replicas}",
            "--output",
            f"/results/scheduler-{replicas}.loadgen.json",
        ],
        detached=False,
    )
    process = subprocess.Popen(
        load_command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    submission_started = time.monotonic()
    submission_deadline = submission_started + args.duration
    while process.poll() is None and time.monotonic() < submission_deadline:
        if durable_run_count() >= args.jobs:
            break
        time.sleep(2)
    if durable_run_count() < args.jobs:
        process.terminate()
        stdout, stderr = process.communicate(timeout=30)
        raise RuntimeError(
            f"job prefill failed: runs={durable_run_count()} exit={process.returncode} "
            f"stdout={stdout[-1000:]} stderr={stderr[-1000:]}"
        )

    submission_seconds = time.monotonic() - submission_started
    started = time.monotonic()
    start_schedulers(replicas)
    samples: list[dict[str, Any]] = []
    while process.poll() is None:
        samples.append(sample(time.monotonic() - started))
        time.sleep(args.sample_seconds)
    stdout, stderr = process.communicate()
    scheduler_drain_seconds = time.monotonic() - started
    samples.append(sample(time.monotonic() - started))
    loadgen = parse_loadgen(stdout)
    audit = loadgen.get("audit", {})
    attempt_count = sum(audit.get("attempt_states", {}).values())
    duplicate_executions, expired_unacknowledged = attempt_delivery_audit()
    report = {
        "scheduler_processes": replicas,
        "recorded_at": datetime.now(UTC).isoformat(),
        "exit_code": process.returncode,
        "submission_seconds": round(submission_seconds, 3),
        "scheduler_drain_seconds": round(scheduler_drain_seconds, 3),
        "effective_placements_per_second": round(args.jobs / scheduler_drain_seconds, 3),
        "stderr_tail": stderr[-4000:],
        "loadgen": loadgen,
        "peaks": peaks(samples),
        "rates_per_second": {
            "placements": counter_rate(samples, "placements"),
            "heartbeats": counter_rate(samples, "heartbeats"),
        },
        "correctness": {
            "attempts": attempt_count,
            "retry_attempts": max(0, attempt_count - args.jobs),
            "duplicate_executions": duplicate_executions,
            "expired_unacknowledged_offers": expired_unacknowledged,
            "runs_with_retries": audit.get("runs_with_retries"),
            "runs_lost": audit.get("runs_lost"),
            "runs_non_terminal": audit.get("runs_non_terminal"),
            "reservation_leaks": audit.get("leaked_reservations"),
            "unpublished_outbox_events": audit.get("unpublished_outbox_events"),
        },
    }
    path = args.output_dir / f"scheduler-{replicas}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--replicas", default="1,2,4,8")
    result.add_argument("--workers", type=int, default=50000)
    result.add_argument("--fleet-shards", type=int, default=8)
    result.add_argument("--fleet-duration", type=int, default=3600)
    result.add_argument("--jobs", type=int, default=10000)
    result.add_argument("--duration", type=int, default=1200)
    result.add_argument("--register-timeout", type=int, default=600)
    result.add_argument("--sample-seconds", type=float, default=5)
    result.add_argument("--min-duration-ms", type=int, default=50)
    result.add_argument("--max-duration-ms", type=int, default=500)
    result.add_argument(
        "--reuse-fleet",
        action="store_true",
        help="reuse an already connected fleet and leave it running after the sweep",
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results" / "scheduler-scale",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    replicas = [int(value) for value in args.replicas.split(",")]
    if any(value not in {1, 2, 4, 8} for value in replicas):
        raise ValueError("scheduler replicas must be selected from 1,2,4,8")
    names: list[str] = []
    reports: list[dict[str, Any]] = []
    try:
        reset_workload(max(args.jobs * 2, 100000))
        if args.reuse_fleet:
            durable = durable_worker_count()
            active = prometheus('sum(agent_fabric_active_worker_streams{job="gateways"})') or 0
            if durable < args.workers or active < args.workers:
                raise RuntimeError(
                    f"reusable fleet is too small: durable={durable} active={active}"
                )
        else:
            command(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "agent_fabric",
                    "-d",
                    "agent_fabric",
                    "-c",
                    "TRUNCATE TABLE workers CASCADE",
                ]
            )
            names = start_fleet(args)
        for replica_count in replicas:
            reports.append(run_workload(args, replica_count))
    finally:
        for name in names:
            command(["docker", "rm", "-f", name], check=False)
        configure_schedulers(1)
    summary = {
        "workers": args.workers,
        "jobs_per_run": args.jobs,
        "runs": [
            {
                "scheduler_processes": report["scheduler_processes"],
                "placements_per_second": report["effective_placements_per_second"],
                "drain_seconds": report["scheduler_drain_seconds"],
                "submission_seconds": report["submission_seconds"],
                "correctness": report["correctness"],
            }
            for report in reports
        ],
    }
    path = args.output_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
