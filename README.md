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
4. **Next bottleneck: the scheduler is serial.** One placement per transaction, reloading 500 candidates and every healthy worker each time, plateaus at 14-21 placements/s regardless of fleet size. Documented, deliberately not redesigned in the same change.

Worker-loss chaos (10% of a 1,000-worker fleet killed mid-run, 20-40 s jobs):

CHAOS_PLACEHOLDER

Hostile sandbox workloads (memory bomb, fork bomb, infinite loop, disk exhaustion, forbidden network) have a runner in `tests/security/run_hostile.py` but have **not** been executed yet: they need a Linux Docker host with `runsc`.

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

See [`benchmarks/README.md`](benchmarks/README.md) for the native runner that also samples process RSS/CPU and `pg_stat_statements`.

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

See [architecture](docs/architecture.md), [scheduler semantics](docs/scheduler.md), [failure handling](docs/failures.md), [sandboxing](docs/sandboxing.md), [threat model](docs/threat-model.md), and [scaling methodology](docs/scaling.md).

## Limitations

- Public HTTPS Git repositories only; no submodules, LFS, private credentials, or secret injection.
- Runtime egress is either disabled or open. There is no domain allowlist claim.
- The scheduler and gRPC gateway are single active instances; the gateway is measured to saturate at roughly 4,000 workers per core and the scheduler at 14-21 placements/s.
- The workspace byte limit is represented in the lease but requires a quota-enabled Linux worker filesystem for hard enforcement; memory, PID, CPU, timeout, root filesystem, and network controls are enforced by the sandbox launch.
- Hostile-workload containment is scripted but not yet measured on a `runsc` host.
- Firecracker, Terraform/AWS deployment, and the gateway/scheduler redesign are deferred phases.
