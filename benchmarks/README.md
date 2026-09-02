# Benchmarks

Two runners exist. Both drive the real control plane through the same gRPC and
HTTP contracts a Go worker and an API client use; nothing is mocked.

| Runner | Where it runs | What it records |
|---|---|---|
| `run_native.py` | A Linux host with PostgreSQL 16 (+ `pg_stat_statements`), Redis, and MinIO installed natively | Per-tier JSON: environment facts, per-process peak RSS and CPU, 1 s samples of queue depth and healthy workers, the load generator's client-side view, the PostgreSQL audit of every submitted run, top statements from `pg_stat_statements`, and Redis counters |
| `run_tiers.ps1` | Docker Desktop via `docker compose --profile load` | The load generator's JSON only (client view plus the PostgreSQL audit) |
| `run_docker_scale.py` | Docker Desktop | Clean-state escalation with container CPU/RSS/PIDs, PostgreSQL/Redis counters, Prometheus gateway and scheduler measurements, and automatic stop conditions |

`report.py` renders any directory of `run_native.py` results as Markdown. Every
number in a report is copied from a result file.

Two more harnesses feed the reports. `tests/chaos/run_scenarios.py` reuses the
simulated fleet to inject one fault per run (a killed worker, 10% of the fleet, a
scheduler restart, a PostgreSQL restart, or a Redis restart) and computes detection,
requeue, recovery, and loss from PostgreSQL timestamps; `tests/chaos/run_native.sh`
wraps it with the host-specific restart commands. `tests/security/run_hostile.py`
submits the hostile workloads through the API to a real `runsc` worker and records
what the sandbox did.

Committed reports live in `reports/`: `2026-09-01-native-4c16g` (baseline, first
bottleneck, worker-loss chaos) and `2026-09-02-batch-scheduler` (the scheduler
redesign measured against it, fault scenarios beyond worker loss, and the hostile
workloads run for real). `2026-09-02-gpu-scale` records the mixed CPU/GPU fleet,
gateway ceiling, and post-fix worker-loss measurements. Raw output written to
`results/` stays ignored by Git so that only deliberately published runs are
versioned.

## Method

- Simulated workers register with 8,000 cpu-millis, 16,384 MB, and 4,096 PIDs
  each and heartbeat every 5 s; jobs request 100 cpu-millis / 128 MB / 16 PIDs
  and run for a seeded uniform 50–500 ms. This isolates control-plane behaviour
  from sandbox behaviour by design.
- Every tier starts from truncated tables, a flushed Redis, reset
  `pg_stat_statements`, and freshly started API, gateway, and scheduler
  processes, so tiers do not contaminate each other and RSS is per tier.
- Jobs are submitted with concurrency 100. HTTP 429 (project admission limit)
  is treated as the designed backpressure signal and retried with exponential
  backoff; the count is reported.
- The load generator waits until every submitted run reaches a terminal state
  in PostgreSQL or the duration budget expires. Not draining within budget is
  reported, not hidden.
- Latencies come from PostgreSQL timestamps (`created_at`, `started_at`,
  `finished_at`), not from client clocks. Lease latency (submission to lease
  receipt at the worker) is the one client-side measurement.
- The audit also sums `reserved_*` on the workers table after the run. With
  every run terminal the sum must be zero; any other value is a resource
  accounting bug and is reported as "leaked reservations".
- Worker-loss chaos: `--kill-fraction F --kill-after-seconds T` selects the
  busiest workers by default (or a seeded random set with `--kill-selection random`)
  T seconds after submission finishes and cancels
  their streams without completion, cleanup, or further heartbeats. Detection
  is the time from the kill to the attempt being marked `LOST`; recovery is the
  time from the kill to the affected run reaching a terminal state elsewhere.

## Running natively

```bash
pip install -e ".[dev]"
export DATABASE_URL=postgresql+asyncpg://agent_fabric:agent_fabric@localhost:5432/agent_fabric
export REDIS_URL=redis://localhost:6379/0 MINIO_ENDPOINT=localhost:9000
psql "$DATABASE_URL" -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'  # needs shared_preload_libraries
python benchmarks/run_native.py --tiers 100,1000,10000 --jobs 10000 --duration 600 --label baseline
python benchmarks/run_native.py --tiers 1000 --jobs 5000 --kill-fraction 0.1 --kill-after-seconds 5 --label worker-loss
python benchmarks/report.py benchmarks/results --output benchmarks/results/REPORT.md
```

The runner raises the seeded project's admission limits (`PROJECT_MAX_QUEUED`,
`PROJECT_MAX_RUNNING`) so that per-tenant fairness caps do not masquerade as
scheduler throughput; both values are recorded in the result file.

## Running through Docker Compose

```powershell
docker compose up --build -d
./benchmarks/run_tiers.ps1 -Tiers 100,1000 -Jobs 10000 -Duration 600
python benchmarks/run_docker_scale.py --tiers 10000,25000,50000,100000 --jobs 10000
```

Set `PROJECT_MAX_QUEUED` and `PROJECT_MAX_RUNNING` in `.env` first, otherwise
the default limits (1,000 queued, 20 running) dominate the result.

## Stop conditions

Escalation stops when memory exceeds 80% of the host, when swap activity
affects results, when the error rate exceeds 1%, or when the control plane
cannot recover. The 100,000 and 1,000,000 tiers are opt-in experiments, not
acceptance claims.
