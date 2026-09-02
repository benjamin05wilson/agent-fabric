# Agent Fabric

Agent Fabric is a lease-based execution control plane for running untrusted repository workloads on resource-aware workers. It deliberately keeps the agent payload simple so scheduling, failure recovery, sandboxing, observability, and scale behaviour remain visible.

This repository contains an executable baseline plus the first measured evidence about it. Every number below comes from a result file committed under [`benchmarks/reports`](benchmarks/reports); nothing is a projection.

## Measured results (2026-09-01, one 4-core host)

Full narrative and raw data: [`benchmarks/reports/2026-09-01-native-4c16g`](benchmarks/reports/2026-09-01-native-4c16g/README.md). Simulated workers, 10,000 jobs per tier, 600 s budget per tier after submission starts.

| Workers | Revision | Drained in budget | Placements/s | Wasted lease offers | Reservation leak after run | End-to-end p50 / p99 |
|---|---|---|---|---|---|---|
| 100 | baseline | yes, 529 s | 20.7 | 951 of 10,951 | 62,200 cpu-millis (7.8 workers' capacity) | 257 s / 457 s |
| 100 | after fix | yes, 478 s | 20.9 | 0 of 10,000 | 0 | 202 s / 404 s |
| 1,000 | baseline | no, 1,430 open at 684 s | 14.1 | 1,025 of 9,673 | 68,300 cpu-millis (net of 73 runs still in flight) | 345 s / 605 s |
| 1,000 | after fix | no, 557 open at 683 s | 14.3 | 185 of 9,764 | 0 | 307 s / 593 s |
| 10,000 | baseline | collapse: 3,972 registered, 0 runs finished | 0 | 11,140 of 11,186 | n/a | never |
| 10,000 | after fix | collapse: 4,233 registered, 0 runs finished | 0 | 4,143 of 4,183 | n/a | never |

What the baseline found, in the order it was found:

1. **First bottleneck: the outbox publisher was starved inside the API process.** During the submission burst its lag reached 20-43 s while lease offers carry a 10 s acknowledgement deadline, so 9-11% of placements expired before delivery. Fixed by making it a separate pipelined process; post-fix lag is 3-27 ms.
2. **Bug: scheduler lost updates leaked worker reservations.** Locked re-selects reused stale ORM snapshots and overwrote gateway releases. After 10,000 successful runs, phantom reservations equalled nearly eight workers. Fixed with `populate_existing` on the locked re-select; post-fix leak is zero.
3. **Collapse point: the single-process gateway saturates at roughly 4,000 workers.** Each 5 s heartbeat is a PostgreSQL transaction plus a Redis write on one asyncio loop; at ~800 heartbeats/s the gateway uses a full core, registration stalls, lease delivery misses the deadline, and throughput is zero. Not fixed; it is the next redesign target.
4. **Next bottleneck: the scheduler is serial.** One placement per transaction, reloading 500 candidates and every healthy worker each time, plateaus at 14-21 placements/s regardless of fleet size. Documented, deliberately not redesigned in the same change; redesigned and measured in part 2 below.

Worker-loss chaos (10% of a 1,000-worker fleet killed mid-run, 20-40 s jobs):

| Scenario | In-flight on killed workers | Attempts lost | Runs affected | Runs lost | Detection p50 / max | Recovery p50 / p99 |
|---|---|---|---|---|---|---|
| Random 10% | 136 | 174 | 160 | 0 | 13.2 s / 19.2 s | 50.8 s / 61.3 s |
| Busiest 10% first | 473 | 629 | 597 | 0 | 12.0 s / 22.1 s | 61.2 s / 85.6 s |

Every affected run was requeued and finished on a surviving worker, no run needed a third attempt, and the reservation audit was zero afterwards. Recovery time is re-execution plus queue wait; detection is the configured 10 s offer deadline or 15 s heartbeat window plus reconcile latency.

Hostile sandbox workloads (memory bomb, fork bomb, infinite loop, disk exhaustion, forbidden network) were executed for the first time in part 2 below.

## Measured results, part 2 (2026-09-02, same host): the scheduler redesign

Full narrative and raw data: [`benchmarks/reports/2026-09-02-batch-scheduler`](benchmarks/reports/2026-09-02-batch-scheduler/README.md). Same runner, workload, seed, and host as part 1; "before" is part 1's `after fix` row.

| Workers | Scheduler | Drained in budget | Placements/s | Wasted lease offers | Runs lost | Time-to-start p50 / p99 |
|---|---|---|---|---|---|---|
| 100 | serial (before) | yes, 478 s | 20.9 | 0 of 10,000 | 0 | 202 s / 403 s |
| 100 | batch, bound 500 | **yes, 110 s** | 90.8 | 0 of 10,000 | 0 | 23.5 s / 40.1 s |
| 1,000 | serial (before) | no, 557 open at 683 s | 14.3 | 185 of 9,764 | 0 | 306 s / 593 s |
| 1,000 | batch, bound 500 | **yes, 284 s** | 50.4 | 4,666 of 14,666 (32%) | 0 | 114 s / 217 s |
| 1,000 | batch, bound 100 | **yes, 274 s** | 36.5 | 0 of 10,000 | 0 | 118 s / 207 s |
| 10,000 | batch | collapse, unchanged: 3,712 registered, 0 runs finished | 0 | 30,385 (all) | 0 | never |

What this change found:

1. **Batch placement removes the scheduler ceiling.** Reading candidates and worker capacity once per iteration and writing placements in bulk takes the 100-worker tier from 478 s to 110 s and lets the 1,000-worker tier drain for the first time. The scheduler process drops from 74% to 16-41% of a core.
2. **A faster scheduler without backpressure loses work.** The first batch version had no bound on unacknowledged offers, out-ran the gateway, and turned 1,127 of 10,000 runs into `LOST` with every worker healthy. The scheduler now caps offers in the `OFFERED` state (`SCHEDULER_MAX_OUTSTANDING_OFFERS`); the bound must track gateway acknowledgement throughput, which a static 500 does not at 1,000 workers (32% of offers wasted) and 100 does.
3. **The gateway is now the limit at every tier.** 93-99% of a core at 100 and 1,000 workers, and the 10,000-worker collapse (part 1, finding 3) is untouched. Per-heartbeat persistence in one process is the next redesign.

Fault scenarios beyond worker loss (200 workers, 2,000 retry-safe jobs, fresh control plane per scenario):

| Scenario | Affected runs | Detection | Recovery | Runs lost | Unfinished at 300 s |
|---|---|---|---|---|---|
| Kill one worker (70 running attempts) | 82 | 9.9 s | 22.3 s | 0 | 0 |
| Kill 10% of the fleet (249 running attempts) | 608 | 9.6 s | 23.6 s | 0 | 0 |
| Scheduler restart | 0 | n/a | 1.4 s placement gap | 0 | 0 |
| PostgreSQL fast restart | 468 | 9.2 s | 24.1 s | 0 | 0 |
| Redis restart | 500 | 9.4 s | **never** | 0 | **1,583** |

Worker loss and PostgreSQL restarts lose nothing. A Redis restart still livelocks delivery: the outbox publisher now survives it, but every gateway stream's Redis dispatcher dies with the connection and is never recreated, so no connected worker gets another offer until the gateway restarts. Measured, not yet fixed.

Hostile workloads, executed for the first time through the real Go worker under `runsc` (Docker 29.3, gVisor release channel, systrap platform):

| Workload | Outcome | Contained |
|---|---|---|
| Escape probe | uid 65532, no capabilities, gVisor kernel, root read-only, only `/workspace` writable, no Docker socket | yes |
| Memory bomb (128 MB) | OOM-killed, exit 137 | yes |
| Fork bomb (32 PIDs) | sandbox terminated at the PID limit | yes |
| Infinite loop (10 s) | `TIMED_OUT` at 12 s | yes |
| `/tmp` exhaustion (64 MiB tmpfs) | `ENOSPC` at 64 MiB | yes |
| Forbidden network | DNS and raw TCP both unreachable | yes |
| Workspace disk exhaustion | **12.5 GB written in 56 s, host disk full, MinIO refused log writes, gateway dropped the worker stream, worker died without cleanup, run `LOST`** | **no** |

Two worker fixes came out of it: gVisor needs PID headroom (a 16-PID limit cannot even start the sandbox; the worker now adds a measured overhead of 48 and reports a Docker exit status of 125 as `SANDBOX_START`), and stale per-attempt workspaces are purged when the worker starts.

Deployment: [`infra/terraform`](infra/terraform/README.md) creates a small AWS environment (VPC, RDS PostgreSQL, ElastiCache, S3 run logs, one control-plane instance, a gVisor worker auto-scaling group, scoped IAM roles). It passes `terraform validate` and `fmt` and has not been applied to a real account from this repository.

## Architecture

```text
FastAPI -> PostgreSQL + transactional outbox -> outbox publisher -> Redis Streams -> scheduler
                                                                                   |
                                                                       bidirectional gRPC
                                                                                   |
                                                                         Go worker -> runsc

Worker events -> gRPC -> MinIO objects + PostgreSQL indexes
Telemetry     -> OpenTelemetry Collector -> Tempo / Prometheus / Grafana
```

PostgreSQL is authoritative for runs, attempts, leases, and reservations. Redis is a wake-up and delivery layer; reconciliation reconstructs work from durable state after interruptions. The API, gateway, outbox publisher, and scheduler are separate processes; the scheduler and gateway are single active instances.

## Quick start

Requirements are Docker Desktop for the control plane and native Linux or WSL2 with a Docker Engine configured with gVisor for real execution.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
curl.exe http://localhost:8000/health
```

Submit a run after a real or simulated worker registers:

```powershell
$body = @{
  repository = @{ url = "https://github.com/example/project"; ref = "HEAD" }
  argv = @("python", "-m", "pytest", "-q")
  profile = "python"
  network = "disabled"
  resources = @{ cpu_millis = 1000; memory_mb = 512; pids = 128; disk_mb = 1024; timeout_seconds = 300 }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod http://localhost:8000/runs -Method Post `
  -Headers @{ Authorization = "Bearer af_dev_key"; "Idempotency-Key" = "demo-1" } `
  -ContentType application/json -Body $body
```

GPU jobs request device capacity, VRAM, and the matching worker capability:

```json
{
  "profile": "cuda",
  "required_capabilities": ["cuda"],
  "resources": {"gpu": 1, "vram_mb": 8192}
}
```

GPU workers advertise inventory with `WORKER_GPU_COUNT`, `WORKER_VRAM_MB`, and
`WORKER_CAPABILITIES=network-disabled,cuda`. The worker passes GPU count to Docker device
allocation. VRAM is scheduler admission accounting, not a hard per-process VRAM limit.

Run a simulated fleet (the PostgreSQL audit is on because `DATABASE_URL` is set for the service):

```powershell
docker compose --profile load run --rm --build loadgen `
  --control grpc:50051 --api http://api:8000 --workers 100 --jobs 1000 --duration 60
```

Inject worker loss and measure recovery:

```powershell
docker compose --profile load run --rm loadgen `
  --control grpc:50051 --api http://api:8000 --workers 1000 --jobs 3000 `
  --min-duration-ms 20000 --max-duration-ms 40000 `
  --kill-fraction 0.1 --kill-after-seconds 60 --kill-selection busiest
```

See [`benchmarks/README.md`](benchmarks/README.md) for native and Docker runners that also
sample CPU/RSS, PostgreSQL, Redis, gateway, heartbeat, and scheduler behaviour.

## Real gVisor worker

Install `runsc` on a Linux Docker host, run `sudo runsc install`, restart Docker, and verify `docker run --rm --runtime=runsc hello-world`. The worker refuses to register unless that runtime appears in Docker's runtime inventory. Docker Desktop's embedded engine does not currently expose `runsc`; use a native WSL2 Docker Engine or Linux host.

Start the worker profile only on that host:

```bash
docker compose --profile gvisor up --build worker
python tests/security/run_hostile.py --api http://localhost:8000 --output hostile-results.json
```

## Development

```powershell
docker run --rm -v "${PWD}/worker:/src" -w /src golang:1.24-bookworm `
  sh -c "/usr/local/go/bin/go test ./..."
docker build -f control-plane/Dockerfile -t agent-fabric-control .
docker compose config --quiet
```

See [architecture](docs/architecture.md), [scheduler semantics](docs/scheduler.md), [failure handling](docs/failures.md), [sandboxing](docs/sandboxing.md), [threat model](docs/threat-model.md), [scaling methodology](docs/scaling.md), and the [Terraform environment](infra/terraform/README.md).

## Limitations

- Public HTTPS Git repositories only; no submodules, LFS, private credentials, or secret injection.
- Runtime egress is either disabled or open. There is no domain allowlist claim.
- The scheduler and gRPC gateway are single active instances; the gateway is measured to saturate at roughly 4,000 workers per core and is now the throughput limit at every tier. The scheduler's outstanding-offer bound is a static setting that needs hand-tuning per fleet size.
- A Redis restart stops lease delivery until the gateway restarts (measured); a full disk drops worker streams and leaks the run (measured).
- The workspace byte limit is represented in the lease but requires a quota-enabled Linux worker filesystem for hard enforcement; memory, PID, CPU, timeout, root filesystem, and network controls are enforced by the sandbox launch.
- Hostile-workload containment is scripted but not yet measured on a `runsc` host.
- The Terraform environment under `infra/terraform/` passes `validate` and `fmt` but has not been applied to a real AWS account from this repository.
- Firecracker and the gateway redesign are deferred phases.
