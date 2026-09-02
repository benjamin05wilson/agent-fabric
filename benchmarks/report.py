#!/usr/bin/env python
"""Render Markdown tables from ``benchmarks/run_native.py`` result files.

Usage: ``python benchmarks/report.py <results-dir> [--output REPORT.md]``.
Every number in the output comes from a result file; nothing is estimated.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(results_dir: Path) -> list[dict[str, Any]]:
    results = []
    for path in sorted(results_dir.glob("*.json")):
        if path.name.endswith(".loadgen.json"):
            continue
        data = json.loads(path.read_text())
        if "tier" in data and "loadgen" in data:
            data["_file"] = path.name
            results.append(data)
    return sorted(results, key=lambda item: (item.get("label", ""), item["tier"]))


def fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def pct(block: dict[str, Any] | None, key: str, digits: int = 0) -> str:
    if not block:
        return "n/a"
    return fmt(block.get(key), digits)


def environment_section(results: list[dict[str, Any]]) -> list[str]:
    if not results:
        return []
    env = results[0]["environment"]
    lines = ["## Environment", ""]
    for key in (
        "git_revision",
        "git_dirty",
        "hostname",
        "kernel",
        "cpu_model",
        "cpu_count",
        "memory_mb",
        "python",
        "postgres",
        "redis",
        "note",
    ):
        lines.append(f"- {key}: `{env.get(key)}`")
    lines.append("")
    return lines


def scaling_table(results: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Scaling tiers",
        "",
        "| Label | Workers | Jobs accepted | 429s | Registered in (s) | Drained | Drain (s) |"
        " Placements/s | Completion/s | Time-to-start p50/p95/p99 (ms) |"
        " End-to-end p50/p95/p99 (ms) | Lost | Non-terminal | Leaked CPU (millis) |"
        " Peak RSS api/grpc/outbox/sched (MB) | CPU api/grpc/outbox/sched (% of one core) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        loadgen = item.get("loadgen") or {}
        res = loadgen.get("results") or {}
        audit = loadgen.get("audit") or {}
        submission = res.get("submission") or {}
        drain = res.get("drain") or {}
        metrics = item.get("scheduler_metrics") or {}
        processes = item.get("processes") or {}
        drain_seconds = drain.get("seconds_after_start")
        # The batch scheduler places many runs per iteration; prefer its placement counter
        # and fall back to iteration count for results recorded by the serial scheduler.
        placements = metrics.get("agent_fabric_placements_total") or metrics.get(
            "agent_fabric_scheduling_seconds_count"
        )
        placements_per_second = placements / drain_seconds if placements and drain_seconds else None
        lines.append(
            "| {label} | {tier} | {accepted} | {backpressure} | {registered} | {drained} |"
            " {drain} | {placements} | {completion} | {tts} | {e2e} | {lost} | {open} |"
            " {leak} | {rss} | {cpu} |".format(
                label=item.get("label") or "-",
                tier=fmt(item["tier"]),
                accepted=fmt(submission.get("accepted")),
                backpressure=fmt(submission.get("backpressure_429")),
                registered=fmt(res.get("registered_after_seconds")),
                drained="yes" if drain.get("drained") else "no",
                drain=fmt(drain_seconds),
                placements=fmt(placements_per_second),
                completion=fmt(res.get("completion_throughput_per_second")),
                tts="/".join(
                    pct(audit.get("time_to_start_ms"), key) for key in ("p50", "p95", "p99")
                ),
                e2e="/".join(pct(audit.get("end_to_end_ms"), key) for key in ("p50", "p95", "p99")),
                lost=fmt(audit.get("runs_lost")),
                open=fmt(audit.get("runs_non_terminal")),
                leak=fmt((audit.get("leaked_reservations") or {}).get("cpu_millis")),
                rss="/".join(
                    fmt((processes.get(name) or {}).get("peak_rss_mb"), 0)
                    for name in ("api", "grpc", "outbox", "scheduler")
                ),
                cpu="/".join(
                    fmt((processes.get(name) or {}).get("cpu_percent_of_one_core"), 0)
                    for name in ("api", "grpc", "outbox", "scheduler")
                ),
            )
        )
    lines.append("")
    return lines


def chaos_table(results: list[dict[str, Any]]) -> list[str]:
    rows = [
        item for item in results if ((item.get("loadgen") or {}).get("results") or {}).get("chaos")
    ]
    if not rows:
        return []
    lines = [
        "## Worker-loss chaos",
        "",
        "| Label | Workers | Killed | Selection | In-flight at kill | Lost attempts |"
        " Affected runs | Requeued and finished | Runs LOST | Detection p50/max (s) |"
        " Recovery p50/p99/max (s) | Drained | Leaked CPU (millis) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in rows:
        res = item["loadgen"]["results"]
        chaos = res["chaos"]
        audit = item["loadgen"].get("audit") or {}
        recovery = audit.get("recovery") or {}
        drain = res.get("drain") or {}
        lines.append(
            "| {label} | {tier} | {killed} | {selection} | {inflight} | {lost_attempts} |"
            " {affected} | {requeued} | {lost} | {detect} | {recover} | {drained} |"
            " {leak} |".format(
                label=item.get("label") or "-",
                tier=fmt(item["tier"]),
                killed=fmt(chaos.get("killed_workers")),
                selection=chaos.get("selection", "random"),
                inflight=fmt(chaos.get("in_flight_attempts_at_kill")),
                lost_attempts=fmt(recovery.get("lost_attempts_on_killed_workers")),
                affected=fmt(recovery.get("affected_runs")),
                requeued=fmt(recovery.get("requeued_and_finished_elsewhere")),
                lost=fmt(recovery.get("runs_lost")),
                detect="/".join(
                    pct(recovery.get("detection_seconds"), key, 1) for key in ("p50", "max")
                ),
                recover="/".join(
                    pct(recovery.get("recovery_seconds"), key, 1) for key in ("p50", "p99", "max")
                ),
                drained="yes" if drain.get("drained") else "no",
                leak=fmt((audit.get("leaked_reservations") or {}).get("cpu_millis")),
            )
        )
    lines.append("")
    return lines


def statements_section(results: list[dict[str, Any]], limit: int) -> list[str]:
    lines: list[str] = []
    for item in results:
        statements = item.get("postgres_top_statements") or []
        if not statements:
            continue
        lines += [
            f"### PostgreSQL statements: {item.get('label') or '-'} / {fmt(item['tier'])} workers",
            "",
            "| Calls | Total ms | Mean ms | Max ms | Rows | Query |",
            "|---|---|---|---|---|---|",
        ]
        for row in statements[:limit] + [statements[-1]]:
            query = str(row.get("query", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {fmt(row.get('calls'))} | {row.get('total_ms')} | {row.get('mean_ms', '')} |"
                f" {row.get('max_ms', '')} | {row.get('rows', '')} | `{query[:110]}` |"
            )
        lines.append("")
    return lines


def redis_section(results: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Redis",
        "",
        "| Label | Workers | Connected clients | Blocked | Peak memory | Commands | Rejected |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in results:
        redis = item.get("redis") or {}
        lines.append(
            f"| {item.get('label') or '-'} | {fmt(item['tier'])} | {redis.get('connected_clients')}"
            f" | {redis.get('blocked_clients')} | {redis.get('used_memory_peak_human')}"
            f" | {redis.get('total_commands_processed')} | {redis.get('rejected_connections')} |"
        )
    lines.append("")
    return lines


def timeline_section(results: list[dict[str, Any]]) -> list[str]:
    lines = ["## Queue depth and healthy workers over time (10 s samples)", ""]
    for item in results:
        samples = item.get("samples") or []
        if not samples:
            continue
        lines += [
            f"### {item.get('label') or '-'} / {fmt(item['tier'])} workers",
            "",
            "| t (s) | Queue depth | Healthy workers | Placements so far | Outbox lag (s) |"
            " Scheduler RSS (MB) | Gateway RSS (MB) |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in samples[::10] + ([samples[-1]] if len(samples) % 10 != 1 else []):
            lines.append(
                f"| {row.get('t')} | {fmt(row.get('queue_depth'), 0)} |"
                f" {fmt(row.get('healthy_workers'), 0)} |"
                f" {fmt(row.get('placements', row.get('scheduling_count')), 0)} |"
                f" {fmt(row.get('outbox_lag_seconds'), 1)} |"
                f" {fmt(row.get('scheduler_rss_mb'), 0)} | {fmt(row.get('grpc_rss_mb'), 0)} |"
            )
        lines.append("")
    return lines


def render(results_dir: Path, statement_limit: int) -> str:
    results = load(results_dir)
    lines = ["# Benchmark results", "", f"Source directory: `{results_dir}`", ""]
    lines += environment_section(results)
    lines += scaling_table(results)
    lines += chaos_table(results)
    lines += redis_section(results)
    lines += ["## PostgreSQL statement profile (pg_stat_statements)", ""]
    lines += statements_section(results, statement_limit)
    lines += timeline_section(results)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--statements", type=int, default=8)
    args = parser.parse_args()
    text = render(args.results_dir, args.statements)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
