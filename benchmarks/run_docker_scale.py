"""Measured Docker scale runner with safety stop conditions.

Runs each fleet tier against clean durable state and records load-generator audit data,
container CPU/RSS, PostgreSQL/Redis counters, and Prometheus gateway/scheduler metrics.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {"api", "scheduler", "outbox", "postgres", "redis", "loadgen"}
PROMETHEUS = "http://localhost:9090/api/v1/query"


def command(args: list[str], *, check: bool = True) -> str:
    result = subprocess.run(
        args, cwd=ROOT, check=check, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip()


def reset_state(limit: int) -> None:
    sql = (
        "TRUNCATE TABLE run_event_indexes, attempts, runs, outbox_events, workers "
        "RESTART IDENTITY CASCADE; "
        f"UPDATE projects SET max_queued={limit}, max_running={limit};"
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
    command(["docker", "compose", "exec", "-T", "redis", "redis-cli", "FLUSHDB"])


def prometheus(query: str) -> float | None:
    try:
        url = PROMETHEUS + "?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(url, timeout=3) as response:
            results = json.load(response)["data"]["result"]
        return float(results[0]["value"][1]) if results else None
    except (OSError, KeyError, ValueError, IndexError, json.JSONDecodeError):
        return None


def parse_bytes(value: str) -> int:
    match = re.match(r"\s*([0-9.]+)\s*([KMGT]?i?B)", value)
    if not match:
        return 0
    powers = {"B": 0, "KB": 1, "KiB": 1, "MB": 2, "MiB": 2, "GB": 3, "GiB": 3, "TB": 4, "TiB": 4}
    return int(float(match.group(1)) * (1024 ** powers[match.group(2)]))


def container_stats() -> dict[str, dict[str, float]]:
    raw = command(["docker", "stats", "--no-stream", "--format", "{{json .}}"], check=False)
    result: dict[str, dict[str, float]] = {}
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = row.get("Name", "")
        shard = re.search(r"-grpc-([0-7])-", name)
        scheduler_replica = re.search(r"-scheduler-([1-7])-", name)
        if name.startswith("af-scale-"):
            service = "loadgen"
        elif shard:
            service = f"grpc-{shard.group(1)}"
        elif "-grpc-" in name:
            service = "grpc-lb"
        elif scheduler_replica:
            service = f"scheduler-{scheduler_replica.group(1)}"
        else:
            service = next((item for item in SERVICES if f"-{item}-" in name), None)
        if service is None:
            continue
        result[service] = {
            "cpu_percent": float(row.get("CPUPerc", "0%").rstrip("%")),
            "rss_mb": round(parse_bytes(row.get("MemUsage", "0B").split("/")[0]) / 1048576, 3),
            "pids": float(row.get("PIDs", 0)),
        }
    return result


def postgres_stats() -> dict[str, int]:
    sql = (
        "SELECT row_to_json(s) FROM (SELECT numbackends,xact_commit,xact_rollback,"
        "blks_read,blks_hit,temp_bytes,deadlocks,"
        "(SELECT count(*) FROM workers WHERE gpu_count>0) AS gpu_workers,"
        "(SELECT count(*) FROM workers) AS total_workers,"
        "(SELECT coalesce(sum(gpu_count),0) FROM workers) AS total_gpu,"
        "(SELECT coalesce(sum(reserved_gpu_count),0) FROM workers) AS reserved_gpu,"
        "(SELECT count(*) FROM runs WHERE state='QUEUED' AND "
        "coalesce((spec->'resources'->>'gpu')::int,0)=0) AS cpu_queue,"
        "(SELECT count(*) FROM runs WHERE state='QUEUED' AND "
        "coalesce((spec->'resources'->>'gpu')::int,0)>0) AS gpu_queue "
        "FROM pg_stat_database WHERE datname='agent_fabric') s;"
    )
    raw = command(
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
            sql,
        ],
        check=False,
    )
    try:
        return {key: int(value) for key, value in json.loads(raw).items()}
    except (ValueError, json.JSONDecodeError):
        return {}


def redis_stats() -> dict[str, int]:
    raw = command(["docker", "compose", "exec", "-T", "redis", "redis-cli", "INFO"], check=False)
    wanted = {
        "connected_clients",
        "used_memory",
        "used_memory_peak",
        "total_commands_processed",
        "instantaneous_ops_per_sec",
        "rejected_connections",
    }
    result: dict[str, int] = {}
    for line in raw.splitlines():
        key, separator, value = line.rstrip("\r").partition(":")
        if separator and key in wanted:
            try:
                result[key] = int(value)
            except ValueError:
                pass
    return result


def durable_worker_count() -> int:
    raw = command(
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
            "SELECT count(*) FROM workers",
        ],
        check=False,
    )
    try:
        return int(raw)
    except ValueError:
        return 0


def metrics_snapshot() -> dict[str, float | None]:
    return {
        "placements": prometheus('sum(agent_fabric_placements_total{job="schedulers"})'),
        "queue_depth": prometheus('max(agent_fabric_queue_depth{job="schedulers"})'),
        "outstanding_offers": prometheus(
            'max(agent_fabric_outstanding_offers{job="schedulers"})'
        ),
        "active_streams": prometheus(
            'sum(agent_fabric_active_worker_streams{job="gateways"})'
        ),
        "heartbeats": prometheus('sum(agent_fabric_heartbeats_total{job="gateways"})'),
        "gateway_p95_seconds": prometheus(
            "histogram_quantile(0.95,sum by(le)(rate("
            'agent_fabric_gateway_message_seconds_bucket{job="gateways"}[2m])))'
        ),
        "gateway_p99_seconds": prometheus(
            "histogram_quantile(0.99,sum by(le)(rate("
            'agent_fabric_gateway_message_seconds_bucket{job="gateways"}[2m])))'
        ),
    }


def sample(elapsed: float) -> dict[str, Any]:
    return {
        "elapsed_seconds": round(elapsed, 3),
        "containers": container_stats(),
        "postgres": postgres_stats(),
        "redis": redis_stats(),
        "prometheus": metrics_snapshot(),
    }


def parse_loadgen(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        return {"parse_error": True, "stdout": stdout[-4000:]}
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        return {"parse_error": True, "stdout": stdout[-4000:]}


def peaks(samples: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for row in samples:
        for service, values in row["containers"].items():
            peak = result.setdefault(service, {"cpu_percent": 0.0, "rss_mb": 0.0, "pids": 0.0})
            for key in peak:
                peak[key] = max(peak[key], values[key])
    gateway_rows = [
        [
            values
            for service, values in row["containers"].items()
            if re.fullmatch(r"grpc-\d", service)
        ]
        for row in samples
    ]
    if gateway_rows:
        result["gateways_total"] = {
            key: max((sum(item[key] for item in row) for row in gateway_rows), default=0.0)
            for key in ("cpu_percent", "rss_mb", "pids")
        }
    scheduler_rows = [
        [
            values
            for service, values in row["containers"].items()
            if service == "scheduler" or re.fullmatch(r"scheduler-\d", service)
        ]
        for row in samples
    ]
    if scheduler_rows:
        result["schedulers_total"] = {
            key: max((sum(item[key] for item in row) for row in scheduler_rows), default=0.0)
            for key in ("cpu_percent", "rss_mb", "pids")
        }
    return result


def counter_rate(samples: list[dict[str, Any]], key: str) -> float | None:
    points = [
        (row["elapsed_seconds"], row["prometheus"].get(key))
        for row in samples
        if row["prometheus"].get(key) is not None
    ]
    if len(points) < 2 or points[-1][0] <= points[0][0]:
        return None
    return round((points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0]), 3)


def run_tier(args: argparse.Namespace, tier: int) -> dict[str, Any]:
    reset_state(max(args.jobs * 2, 100000))
    name = f"af-scale-{tier}"
    output = f"/results/{args.label}-workers-{tier}.loadgen.json"
    load_command = [
        "docker",
        "compose",
        "--profile",
        "load",
        "run",
        "--rm",
        "--name",
        name,
        "loadgen",
        "--control",
        "grpc:50051",
        "--api",
        "http://api:8000",
        "--workers",
        str(tier),
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
        args.label,
        "--output",
        output,
    ]
    if args.kill_fraction:
        load_command += [
            "--kill-fraction",
            str(args.kill_fraction),
            "--kill-after-seconds",
            str(args.kill_after_seconds),
            "--kill-selection",
            args.kill_selection,
        ]
    if args.gpu_workers:
        load_command += [
            "--gpu-workers",
            str(args.gpu_workers),
            "--gpu-count-per-worker",
            str(args.gpu_count_per_worker),
            "--gpu-vram-mb-per-worker",
            str(args.gpu_vram_mb_per_worker),
            "--gpu-job-fraction",
            str(args.gpu_job_fraction),
            "--gpu-job-vram-mb",
            str(args.gpu_job_vram_mb),
        ]
    started = time.monotonic()
    process = subprocess.Popen(
        load_command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    samples: list[dict[str, Any]] = []
    while process.poll() is None:
        samples.append(sample(time.monotonic() - started))
        time.sleep(args.sample_seconds)
    stdout, stderr = process.communicate()
    samples.append(sample(time.monotonic() - started))
    loadgen = parse_loadgen(stdout)
    audit = loadgen.get("audit", {})
    results = loadgen.get("results", {})
    attempts = sum(audit.get("attempt_states", {}).values()) or 0
    lost_attempts = audit.get("attempt_states", {}).get("LOST", 0)
    loss_rate = lost_attempts / attempts if attempts else 0.0
    stream_errors = results.get("stream_errors", 0)
    error_rate = stream_errors / tier if tier else 0.0
    workers_registered = int(audit.get("workers_in_table") or durable_worker_count())
    max_memory_mb = max(
        (sum(item["rss_mb"] for item in row["containers"].values()) for row in samples),
        default=0.0,
    )
    memory_ratio = max_memory_mb * 1048576 / args.docker_memory_bytes
    report = {
        "tier": tier,
        "recorded_at": datetime.now(UTC).isoformat(),
        "exit_code": process.returncode,
        "stderr_tail": stderr[-4000:],
        "loadgen": loadgen,
        "samples": samples,
        "peaks": peaks(samples),
        "rates_per_second": {
            "placements": counter_rate(samples, "placements"),
            "heartbeats": counter_rate(samples, "heartbeats"),
        },
        "stop_conditions": {
            "lease_attempt_loss_rate": round(loss_rate, 6),
            "worker_stream_error_rate": round(error_rate, 6),
            "durable_workers_registered": workers_registered,
            "docker_memory_ratio": round(memory_ratio, 6),
            "triggered": (
                process.returncode != 0
                or loss_rate > 0.01
                or error_rate > 0.01
                or workers_registered < tier
                or memory_ratio > 0.8
            ),
        },
    }
    path = args.output_dir / f"{args.label}-workers-{tier}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Instrumented Docker fleet scale benchmark")
    result.add_argument("--tiers", default="10000,25000,50000,100000")
    result.add_argument("--jobs", type=int, default=10000)
    result.add_argument("--duration", type=int, default=600)
    result.add_argument("--register-timeout", type=int, default=600)
    result.add_argument("--min-duration-ms", type=int, default=50)
    result.add_argument("--max-duration-ms", type=int, default=500)
    result.add_argument("--sample-seconds", type=float, default=5)
    result.add_argument("--kill-fraction", type=float, default=0.0)
    result.add_argument("--kill-after-seconds", type=float, default=10)
    result.add_argument("--kill-selection", choices=["random", "busiest"], default="busiest")
    result.add_argument("--gpu-workers", type=int, default=0)
    result.add_argument("--gpu-count-per-worker", type=int, default=1)
    result.add_argument("--gpu-vram-mb-per-worker", type=int, default=16384)
    result.add_argument("--gpu-job-fraction", type=float, default=0.0)
    result.add_argument("--gpu-job-vram-mb", type=int, default=8192)
    result.add_argument("--label", default="docker-scale")
    result.add_argument("--output-dir", type=Path, default=ROOT / "benchmarks" / "results")
    return result


def main() -> None:
    args = parser().parse_args()
    args.output_dir = args.output_dir.resolve()
    tiers = [int(value) for value in args.tiers.split(",")]
    docker_memory_bytes = int(command(["docker", "info", "--format", "{{.MemTotal}}"]))
    args.docker_memory_bytes = docker_memory_bytes
    environment = {
        "git_revision": command(["git", "rev-parse", "HEAD"]),
        "git_dirty": bool(command(["git", "status", "--porcelain"])),
        "host": platform.node(),
        "platform": platform.platform(),
        "docker_cpus": int(command(["docker", "info", "--format", "{{.NCPU}}"])),
        "docker_memory_bytes": docker_memory_bytes,
        "gateway_shards": 8,
    }
    summary: dict[str, Any] = {"environment": environment, "tiers": []}
    for tier in tiers:
        report = run_tier(args, tier)
        summary["tiers"].append({"tier": tier, "stop_conditions": report["stop_conditions"]})
        if report["stop_conditions"]["triggered"]:
            summary["stopped_before"] = next(
                (candidate for candidate in tiers if candidate > tier), None
            )
            break
    path = args.output_dir / f"{args.label}-summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
