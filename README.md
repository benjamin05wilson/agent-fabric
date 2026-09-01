# Agent Fabric

Agent Fabric is a lease-based execution control plane for running untrusted repository workloads on resource-aware workers. It deliberately keeps the agent payload simple so scheduling, failure recovery, sandboxing, observability, and scale behaviour remain visible.

This repository contains an executable baseline, not a hyperscale claim. Benchmark tables remain empty until a benchmark is actually run on identified hardware.

## Architecture

```text
FastAPI -> PostgreSQL + transactional outbox -> Redis Streams -> scheduler
                                                               |
                                                   bidirectional gRPC
                                                               |
                                                     Go worker -> runsc

Worker events -> gRPC -> MinIO objects + PostgreSQL indexes
Telemetry     -> OpenTelemetry Collector -> Tempo / Prometheus / Grafana
```

PostgreSQL is authoritative for runs, attempts, leases, and reservations. Redis is a wake-up and delivery layer; reconciliation reconstructs work from durable state after interruptions.

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

Run a simulated fleet:

```powershell
docker compose --profile load run --rm --build loadgen `
  --control grpc:50051 --api http://api:8000 --workers 100 --jobs 1000 --duration 60
```

## Real gVisor worker

Install `runsc` on a Linux Docker host, run `sudo runsc install`, restart Docker, and verify `docker run --rm --runtime=runsc hello-world`. The worker refuses to register unless that runtime appears in Docker's runtime inventory. Docker Desktop's embedded engine does not currently expose `runsc`; use a native WSL2 Docker Engine or Linux host.

Start the worker profile only on that host:

```bash
docker compose --profile gvisor up --build worker
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
- The baseline scheduler and gRPC gateway are single active instances.
- The workspace byte limit is represented in the lease but requires a quota-enabled Linux worker filesystem for hard enforcement; memory, PID, CPU, timeout, root filesystem, and network controls are enforced by the sandbox launch.
- Firecracker, Terraform/AWS deployment, and post-profile redesign are deferred phases.
