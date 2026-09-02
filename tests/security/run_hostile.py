#!/usr/bin/env python
"""Submit the hostile workloads to a live control plane and record what the sandbox did.

Run this on a Linux host whose worker registered with a real ``runsc`` runtime:

    python tests/security/run_hostile.py --api http://localhost:8000 --output results.json

Each workload is sent inline through ``python -c`` against a tiny public repository,
with deliberately small limits so the sandbox must enforce them quickly. The script
records the run's terminal state, failure code, exit code, wall-clock time, whether
the worker stayed healthy, and whether cleanup was confirmed, then evaluates each
outcome against the expectation table below. It asserts nothing until the results
exist; ``expected`` is documentation of what containment looks like, not a claim.

Only stdlib is used so the script can run from any Python 3.11+ interpreter.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

WORKLOADS = Path(__file__).resolve().parent / "workloads"

# expected: acceptable terminal states; a run is contained when its state is listed here,
# the worker is still healthy afterwards, and cleanup was confirmed by the worker.
SCENARIOS: dict[str, dict[str, Any]] = {
    "escape_probe": {
        "resources": {"memory_mb": 128, "cpu_millis": 500, "pids": 16, "timeout_seconds": 60},
        "expected": {"SUCCEEDED"},
        "reason": (
            "reports uid, capabilities, no-new-privs, writable paths, devices, and the"
            " gVisor kernel string from inside the sandbox; the log tail is the evidence"
        ),
    },
    "memory_bomb": {
        "resources": {"memory_mb": 128, "cpu_millis": 500, "pids": 32, "timeout_seconds": 60},
        "expected": {"FAILED"},
        "reason": "cgroup memory limit kills the process; exit code is non-zero",
    },
    "fork_bomb": {
        "resources": {"memory_mb": 256, "cpu_millis": 500, "pids": 32, "timeout_seconds": 60},
        "expected": {"FAILED", "TIMED_OUT"},
        "reason": (
            "pids limit stops fork(); under runsc the whole sandbox is terminated (exit 2, no"
            " EAGAIN) once the cgroup limit is reached, so FAILED with no output is the"
            " contained outcome"
        ),
    },
    "infinite_loop": {
        "resources": {"memory_mb": 128, "cpu_millis": 500, "pids": 16, "timeout_seconds": 10},
        "expected": {"TIMED_OUT"},
        "reason": "wall-clock timeout terminates the container",
    },
    "disk_exhaustion": {
        "resources": {"memory_mb": 256, "cpu_millis": 500, "pids": 16, "timeout_seconds": 60},
        "expected": {"FAILED", "TIMED_OUT"},
        "reason": (
            "workspace quota is not hard-enforced (see README limitations); this records"
            " how much was written before the timeout or an I/O error"
        ),
    },
    "tmp_exhaustion": {
        "resources": {"memory_mb": 256, "cpu_millis": 500, "pids": 16, "timeout_seconds": 60},
        "expected": {"FAILED"},
        "reason": "/tmp is a 64 MiB tmpfs, so the writer must hit ENOSPC well before the timeout",
    },
    "forbidden_network": {
        "resources": {"memory_mb": 128, "cpu_millis": 500, "pids": 16, "timeout_seconds": 30},
        "expected": {"FAILED"},
        "reason": "--network=none makes the connection fail immediately",
    },
}


def request(
    api: str, key: str, method: str, path: str, body: dict[str, Any] | None = None, **headers: str
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{api.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def submit(api: str, key: str, name: str, scenario: dict[str, Any], repository: str) -> str:
    source = (WORKLOADS / f"{name}.py").read_text(encoding="utf-8")
    body = {
        "repository": {"url": repository, "ref": "HEAD"},
        "argv": ["python", "-c", source],
        "profile": "python",
        "network": "disabled",
        "resources": {"disk_mb": 128, **scenario["resources"]},
    }
    status, payload = request(
        api, key, "POST", "/runs", body, **{"Idempotency-Key": f"hostile-{name}-{uuid.uuid4().hex}"}
    )
    if status != 202:
        raise SystemExit(f"{name}: submission rejected with {status}: {payload}")
    return str(payload["id"])


def wait(api: str, key: str, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, run = request(api, key, "GET", f"/runs/{run_id}")
        if run.get("state") in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "LOST"}:
            return run
        time.sleep(2)
    _, run = request(api, key, "GET", f"/runs/{run_id}")
    return run


def workers(api: str, key: str) -> list[dict[str, Any]]:
    _, payload = request(api, key, "GET", "/workers")
    return list(payload) if isinstance(payload, list) else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--api-key", default="af_dev_key")
    parser.add_argument("--repository", default="https://github.com/octocat/Hello-World")
    parser.add_argument("--only", nargs="*", default=None, help="subset of scenario names")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    before = {worker["id"]: worker for worker in workers(args.api, args.api_key)}
    if not any(worker["healthy"] for worker in before.values()):
        raise SystemExit("no healthy worker registered; start the gVisor worker first")

    results: dict[str, Any] = {"workers_before": before, "scenarios": {}}
    names = args.only or list(SCENARIOS)
    for name in names:
        scenario = SCENARIOS[name]
        started = time.time()
        run_id = submit(args.api, args.api_key, name, scenario, args.repository)
        budget = scenario["resources"]["timeout_seconds"] + 120
        run = wait(args.api, args.api_key, run_id, budget)
        elapsed = round(time.time() - started, 1)
        _, logs = request(args.api, args.api_key, "GET", f"/runs/{run_id}/logs?limit=50")
        after = {worker["id"]: worker for worker in workers(args.api, args.api_key)}
        attempts = run.get("attempts") or []
        worker_id = attempts[-1]["worker_id"] if attempts else None
        state = run.get("state")
        contained = (
            state in scenario["expected"]
            and worker_id is not None
            and after.get(worker_id, {}).get("healthy") is True
        )
        results["scenarios"][name] = {
            "run_id": run_id,
            "state": state,
            "failure": run.get("failure"),
            "result": run.get("result"),
            "elapsed_seconds": elapsed,
            "worker_id": worker_id,
            "worker_healthy_after": after.get(worker_id, {}).get("healthy") if worker_id else None,
            "worker_reserved_after": after.get(worker_id, {}).get("reserved")
            if worker_id
            else None,
            "expected_states": sorted(scenario["expected"]),
            "expectation": scenario["reason"],
            "contained": contained,
            "log_tail": [record.get("data", "")[-200:] for record in (logs.get("records") or [])][
                -5:
            ],
        }
        print(f"{name}: {state} in {elapsed}s, contained={contained}", file=sys.stderr)

    results["workers_after"] = {worker["id"]: worker for worker in workers(args.api, args.api_key)}
    rendered = json.dumps(results, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not all(item["contained"] for item in results["scenarios"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
