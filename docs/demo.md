# Tiny control-plane demo

This runs **two simulated workers and eight seeded jobs over real gRPC**, with real
PostgreSQL, Redis and MinIO. Workers sleep for a seeded 50–100 ms; they do not clone
repositories, invoke models or create sandboxes. Timing and UUIDs vary between runs.

Requires Python 3.12.13 and Docker Engine with Compose v2 supporting `up --wait`.
The isolated stack publishes **no host ports**; all service traffic stays on its
private Compose network. It runs one gateway and one scheduler. No runsc, GPU,
cloud credentials, external APIs or paid services are needed. Image downloads and
first builds are preparation, outside the five-minute demonstration.

**Validation status:** [Linux CI run 34501897886](https://github.com/benjamin05wilson/agent-fabric/actions/runs/34501897886)
passed this demo and all five service regressions at source `9101a3d` in 27.05 seconds
including cleanup. [Captured transcript/result/metadata](../benchmarks/reports/2026-09-10-readiness/README.md)
retain eight observed client stream errors alongside all eight successful jobs and
zero reservations. Local Python/Go checks passed on macOS; that host has no Docker.
CI uploads actual output under `agent-fabric-tiny-demo-<commit SHA>` on pull requests
and pushes to main.

## Bash

From the repository root, with Python available:

```bash
python3 scripts/demo.py --prepare --output benchmarks/results/preparation
python3 scripts/demo.py --regressions --output benchmarks/results/tiny-demo
```

## PowerShell

The same standard-library runner checks subprocess exit codes and tears down in
`finally`; no Bash-specific traps or timeout executable are needed.

```powershell
python scripts/demo.py --prepare --output benchmarks/results/preparation
if ($LASTEXITCODE -ne 0) { throw "Demo preparation failed" }
python scripts/demo.py --regressions --output benchmarks/results/tiny-demo
if ($LASTEXITCODE -ne 0) { throw "Demo or regressions failed; inspect transcript.txt" }
```

The PowerShell command is documented, not locally Windows-verified. Docker Desktop
must be using Linux containers. All services are independent of the default stack
and existing `.env` settings: a fresh `agent-fabric-demo-<random>` project prevents
old worker rows or jobs from satisfying the gate. Only that project's containers
and volumes are removed. Locally built `agent-fabric-demo-*` images remain reusable.

## What to show in 60–90 seconds

With preparation already complete, run the second command and open its
`result.json`. These are **expected acceptance fields, not captured output**:

```text
results.registration.durable_workers = 2
results.submission.accepted = 8
audit.run_states = {"SUCCEEDED": 8}
audit.attempt_states = {"SUCCEEDED": 8}
audit.reserved_after_run = {"cpu_millis": 0, "memory_mb": 0, "pids": 0, "gpu": 0, "vram_mb": 0}
5 service-backed regression cases pass
metadata.exit_code = 0
```

`--require-success` fails on missing registration, rejected submissions, any
non-successful run/attempt, retries or outstanding reservations. `--deadline 60`
covers registration, submissions (including HTTP 429 retries), draining and audit.
The host runner independently limits startup to 55 s, loadgen to 70 s, optional
regressions to 90 s, and cleanup to 30 s. Including bounded version/provenance
commands, the run budget is at most 295 s with regressions, excluding preparation.
Failures return nonzero. An unavailable/unresponsive Docker daemon can prevent
cleanup; the transcript prints the exact project-scoped command to retry.

Artifacts are `metadata.json` (commit, dirty flag, host/tool versions, exit status),
`result.json` (loadgen JSON on success), and `transcript.txt` (commands, real output,
service logs, cleanup). If loadgen fails, its diagnostic output remains in the
transcript; no successful result is invented. Preserve all three together when
publishing evidence. The seed fixes simulated durations, not scheduling order or
latency; this tiny workload is a correctness demonstration, not a scale benchmark.

## Focused regressions

`--regressions` executes five cases in real PostgreSQL/Redis, each using a unique
PostgreSQL schema and Redis route. Normal `pytest` skips these unless
`AF_SERVICE_TESTS=1`; the Compose runner explicitly enables them and fails if the
container command fails. They assert:

- Concurrent transactions cannot exceed worker capacity, the tenant limit or the
  outstanding-offer limit. A barrier forces overlapping pre-commit snapshots.
- A publish followed by an injected pre-commit crash leaves the outbox row pending.
  Republishing produces two real Redis entries; replayed acknowledgements and
  concurrent duplicate completions preserve one durable attempt and zero final
  reservations.
- Concurrent lease reconciliation releases a retry-safe lost attempt once; its
  late completion cannot release the replacement attempt's reservation. The retry
  succeeds and CPU, memory, PID, GPU and VRAM accounting all return to zero.

These test durable protocol state. They do not prove exactly-once external effects
or execute runsc. See the separately qualified [Linux fixture](real-worker.md).
