#!/usr/bin/env python
"""Run benchmark tiers against a natively installed control plane.

Prerequisites: PostgreSQL (with the ``pg_stat_statements`` extension), Redis, and
MinIO reachable through the usual ``DATABASE_URL``/``REDIS_URL``/``MINIO_*``
environment, plus ``pip install -e .`` so the ``agent-fabric-*`` entry points exist.

For every tier the runner truncates control-plane state, resets Redis and
``pg_stat_statements``, starts fresh API, gateway, and scheduler processes with
metrics enabled, samples their RSS/CPU and the scheduler gauges once a second,
runs the load generator with the PostgreSQL audit, and writes one JSON file.
Nothing is summarised here: ``benchmarks/report.py`` renders the tables.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://agent_fabric:agent_fabric@localhost:5432/agent_fabric"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
API = os.environ.get("BENCH_API", "http://localhost:8000")
CONTROL = os.environ.get("BENCH_CONTROL", "localhost:50051")
SCHEDULER_METRICS = int(os.environ.get("BENCH_SCHEDULER_METRICS_PORT", "9101"))
GRPC_METRICS = int(os.environ.get("BENCH_GRPC_METRICS_PORT", "9102"))
TABLES = ("run_event_indexes", "attempts", "outbox_events", "runs", "workers")


def dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


def sh(command: list[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=False).stdout.strip()


def environment_facts() -> dict[str, Any]:
    cpu_model = ""
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            cpu_model = line.split(":", 1)[1].strip()
            break
    mem_kb = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal"):
            mem_kb = int(line.split()[1])
            break
    return {
        "git_revision": sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "git_dirty": bool(sh(["git", "-C", str(ROOT), "status", "--porcelain"])),
        "hostname": platform.node(),
        "kernel": platform.release(),
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "memory_mb": mem_kb // 1024,
        "python": platform.python_version(),
        "postgres": sh(["psql", dsn(), "-Atc", "SHOW server_version"]),
        "redis": sh(["redis-cli", "INFO", "server"]).split("redis_version:")[-1].split()[0],
        "docker": False,
        "note": "single host; control plane, datastores, and load generator share the CPUs",
    }


async def reset_postgres() -> None:
    connection = await asyncpg.connect(dsn())
    try:
        await connection.execute(f"TRUNCATE {', '.join(TABLES)} CASCADE")
        await connection.execute("SELECT pg_stat_statements_reset()")
    finally:
        await connection.close()


def reset_state() -> None:
    asyncio.run(reset_postgres())
    subprocess.run(["redis-cli", "-u", REDIS_URL, "FLUSHALL"], check=True, capture_output=True)


async def pg_top_statements(limit: int = 15) -> list[dict[str, Any]]:
    connection = await asyncpg.connect(dsn())
    try:
        rows = await connection.fetch(
            "SELECT left(query, 160) AS query, calls,"
            " round(total_exec_time::numeric, 1) AS total_ms,"
            " round(mean_exec_time::numeric, 3) AS mean_ms,"
            " round(max_exec_time::numeric, 1) AS max_ms, rows"
            " FROM pg_stat_statements WHERE query NOT ILIKE '%pg_stat_statements%'"
            " ORDER BY total_exec_time DESC LIMIT $1",
            limit,
        )
        totals = await connection.fetchrow(
            "SELECT sum(calls) AS calls, round(sum(total_exec_time)::numeric, 1) AS total_ms"
            " FROM pg_stat_statements WHERE query NOT ILIKE '%pg_stat_statements%'"
        )
        return [dict(row) for row in rows] + [{"query": "TOTAL", **dict(totals or {})}]
    finally:
        await connection.close()


def redis_info() -> dict[str, Any]:
    info = sh(["redis-cli", "-u", REDIS_URL, "INFO", "all"])
    wanted = {
        "connected_clients",
        "blocked_clients",
        "used_memory_human",
        "used_memory_peak_human",
        "total_commands_processed",
        "instantaneous_ops_per_sec",
        "rejected_connections",
        "maxclients",
    }
    parsed: dict[str, Any] = {}
    for line in info.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key in wanted:
                parsed[key] = value.strip()
    parsed["maxclients"] = sh(
        ["redis-cli", "-u", REDIS_URL, "CONFIG", "GET", "maxclients"]
    ).split()[-1]
    return parsed


class Process:
    def __init__(self, name: str, entry: str, log_dir: Path, extra_env: dict[str, str]) -> None:
        self.name = name
        env = {key: value for key, value in os.environ.items() if key != "METRICS_PORT"}
        env.update(extra_env)
        self.log = open(log_dir / f"{name}.log", "ab")
        self.popen = subprocess.Popen(
            [entry], stdout=self.log, stderr=subprocess.STDOUT, env=env, cwd=ROOT
        )
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        self.peak_rss_mb = 0.0
        self.last_cpu_seconds = 0.0
        self.cpu_seconds = 0.0

    def sample(self) -> dict[str, float]:
        try:
            status = Path(f"/proc/{self.popen.pid}/status").read_text()
            stat = Path(f"/proc/{self.popen.pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            return {"rss_mb": 0.0, "cpu_seconds": self.cpu_seconds}
        rss_mb = 0.0
        for line in status.splitlines():
            if line.startswith("VmRSS"):
                rss_mb = int(line.split()[1]) / 1024
        self.cpu_seconds = (int(stat[11]) + int(stat[12])) / self.clock_ticks
        self.peak_rss_mb = max(self.peak_rss_mb, rss_mb)
        return {"rss_mb": round(rss_mb, 1), "cpu_seconds": round(self.cpu_seconds, 2)}

    def stop(self) -> None:
        if self.popen.poll() is None:
            self.popen.send_signal(signal.SIGTERM)
            try:
                self.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.popen.kill()
        self.log.close()


def scrape(port: int) -> dict[str, float]:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/metrics", timeout=1) as response:
            text = response.read().decode()
    except Exception:
        return {}
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, value = line.rsplit(" ", 1)
        if name.startswith("agent_fabric_"):
            values[name] = float(value)
    return values


def wait_healthy(timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{API}/health", timeout=2) as response:
                if json.loads(response.read())["status"] == "ok":
                    return
        except Exception:
            time.sleep(0.5)
    raise SystemExit("API did not become healthy")


def loadgen_command(args: argparse.Namespace, tier: int, output: Path) -> list[str]:
    command = [
        "agent-fabric-loadgen",
        "--control",
        CONTROL,
        "--api",
        API,
        "--workers",
        str(tier),
        "--jobs",
        str(args.jobs),
        "--duration",
        str(args.duration),
        "--seed",
        str(args.seed),
        "--min-duration-ms",
        str(args.min_duration_ms),
        "--max-duration-ms",
        str(args.max_duration_ms),
        "--register-timeout",
        str(args.register_timeout),
        "--database-url",
        DATABASE_URL,
        "--label",
        args.label,
        "--output",
        str(output),
    ]
    if args.kill_fraction:
        command += [
            "--kill-fraction",
            str(args.kill_fraction),
            "--kill-after-seconds",
            str(args.kill_after_seconds),
        ]
    return command


def run_tier(args: argparse.Namespace, tier: int, results_dir: Path) -> Path:
    name = f"{args.label}-workers-{tier}" if args.label else f"workers-{tier}"
    log_dir = results_dir / "logs" / name
    log_dir.mkdir(parents=True, exist_ok=True)
    reset_state()
    limits = {
        "PROJECT_MAX_QUEUED": str(args.project_max_queued),
        "PROJECT_MAX_RUNNING": str(args.project_max_running),
    }
    processes = {
        "api": Process("api", "agent-fabric-api", log_dir, limits),
    }
    try:
        wait_healthy()
        processes["grpc"] = Process(
            "grpc", "agent-fabric-grpc", log_dir, {"METRICS_PORT": str(GRPC_METRICS)}
        )
        processes["scheduler"] = Process(
            "scheduler", "agent-fabric-scheduler", log_dir, {"METRICS_PORT": str(SCHEDULER_METRICS)}
        )
        time.sleep(2)
        return measure_tier(args, tier, name, results_dir, log_dir, processes)
    finally:
        for process in processes.values():
            process.stop()


def measure_tier(
    args: argparse.Namespace,
    tier: int,
    name: str,
    results_dir: Path,
    log_dir: Path,
    processes: dict[str, Process],
) -> Path:
    loadgen_output = results_dir / f"{name}.loadgen.json"
    samples: list[dict[str, Any]] = []
    started = time.time()
    with open(log_dir / "loadgen.log", "ab") as loadgen_log:
        loadgen = subprocess.Popen(
            loadgen_command(args, tier, loadgen_output),
            stdout=loadgen_log,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
        )
        while loadgen.poll() is None:
            row: dict[str, Any] = {"t": round(time.time() - started, 1)}
            for key, process in processes.items():
                for metric, value in process.sample().items():
                    row[f"{key}_{metric}"] = value
            scheduler_metrics = scrape(SCHEDULER_METRICS)
            row["queue_depth"] = scheduler_metrics.get("agent_fabric_queue_depth")
            row["healthy_workers"] = scheduler_metrics.get("agent_fabric_healthy_workers")
            row["scheduling_count"] = scheduler_metrics.get("agent_fabric_scheduling_seconds_count")
            row["scheduling_sum"] = scheduler_metrics.get("agent_fabric_scheduling_seconds_sum")
            samples.append(row)
            time.sleep(1)
    final_scheduler = scrape(SCHEDULER_METRICS)
    statements = asyncio.run(pg_top_statements())
    redis = redis_info()
    for process in processes.values():
        process.sample()
    loadgen_result = json.loads(loadgen_output.read_text()) if loadgen_output.exists() else None
    elapsed = time.time() - started
    result = {
        "tier": tier,
        "label": args.label,
        "recorded_at": datetime.now(UTC).isoformat(),
        "environment": environment_facts(),
        "runner_configuration": {
            key: value for key, value in vars(args).items() if key not in {"results_dir"}
        },
        "processes": {
            key: {
                "peak_rss_mb": round(process.peak_rss_mb, 1),
                "cpu_seconds": round(process.cpu_seconds, 2),
                "cpu_percent_of_one_core": round(100 * process.cpu_seconds / elapsed, 1),
                "exit_code": process.popen.poll(),
            }
            for key, process in processes.items()
        },
        "scheduler_metrics": {
            key: value
            for key, value in final_scheduler.items()
            if not key.endswith("_created") and "bucket" not in key
        },
        "loadgen": loadgen_result,
        "loadgen_exit_code": loadgen.returncode,
        "postgres_top_statements": statements,
        "redis": redis,
        "samples": samples,
    }
    output = results_dir / f"{name}.json"
    output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(f"tier {tier}: wrote {output}", file=sys.stderr)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tiers", default="100,1000,10000")
    parser.add_argument("--jobs", type=int, default=10000)
    parser.add_argument("--duration", type=int, default=300, help="maximum seconds per tier")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-duration-ms", type=int, default=50)
    parser.add_argument("--max-duration-ms", type=int, default=500)
    parser.add_argument("--register-timeout", type=float, default=120.0)
    parser.add_argument("--project-max-queued", type=int, default=1_000_000)
    parser.add_argument("--project-max-running", type=int, default=1_000_000)
    parser.add_argument("--kill-fraction", type=float, default=0.0)
    parser.add_argument("--kill-after-seconds", type=float, default=5.0)
    parser.add_argument("--label", default="")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "benchmarks" / "results")
    args = parser.parse_args()
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for wanted in (max(soft, 65536), hard):
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, max(hard, wanted)))
            break
        except (ValueError, OSError):
            continue
    print(f"RLIMIT_NOFILE={resource.getrlimit(resource.RLIMIT_NOFILE)}", file=sys.stderr)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    for tier in [int(value) for value in args.tiers.split(",") if value]:
        run_tier(args, tier, args.results_dir)


if __name__ == "__main__":
    main()
