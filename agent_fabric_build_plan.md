# Agent Fabric --- Build Plan

## Objective

Build a portfolio-grade **agent execution platform** that directly
strengthens the main gaps for an Agent Infrastructure role: **gRPC,
sandboxing, scheduling, orchestration, distributed-systems failure
handling, observability, control-plane scaling, and Terraform**.

Use a **small real execution plane plus a large simulated control
plane**. Real workloads run on a few workers; simulated workers test
scheduler/control-plane behaviour at large logical scale.

**Engineering rule:** measure honestly, deliberately break the system,
find the real bottleneck, redesign it, and never claim scale that was
not actually measured.

## Portfolio Story

**QA Platform** --- production systems + Terraform\
→ **Line Command** --- execution environments/runtime internals\
→ **RL Velocity** --- rigorous performance measurement\
→ **Try Mi On** --- resource-constrained local AI execution\
→ **Agent Fabric** --- orchestration, sandboxing, scheduling and
reliable agent execution

Target positioning:

> **I build the infrastructure that lets intelligent software execute
> reliably.**

## Architecture

``` text
                     CONTROL PLANE

                  +-----------------+
                  | FastAPI Gateway |
                  +--------+--------+
                           |
                    Job / Run API
                           |
                  +--------v--------+
                  |    Scheduler    |
                  | resource-aware  |
                  +------+---+------+
                         |   |
                gRPC     |   |     gRPC
               +---------+   +---------+
               v                       v
         +-----------+           +-----------+
         | Worker A  |           | Worker B  |
         +-----+-----+           +-----+-----+
               |                       |
         +-----v------+          +-----v------+
         | gVisor /   |          | gVisor /   |
         | Firecracker|          | Firecracker|
         | sandbox    |          | sandbox    |
         +------------+          +------------+
```

Run lifecycle:

``` text
API request
→ durable job
→ queue
→ scheduler
→ lease
→ gRPC dispatch
→ sandbox
→ repo checkout
→ agent/command
→ logs + metrics
→ result
→ sandbox destruction
→ resource release
```

## 1. Core Execution Flow

A request may specify:

``` yaml
repository: github.com/example/project
task: fix failing test X
cpu: 2
memory: 2GB
timeout: 300
network: restricted
sandbox: gvisor
```

The platform must validate and persist the job, select an eligible
worker, reserve resources, issue a lease, dispatch through gRPC, create
an isolated sandbox, execute the workload, stream events/logs, collect
results, destroy the sandbox, and release resources.

**Keep the agent simple. The infrastructure is the project.**

## 2. FastAPI Control Plane

Initial API:

``` text
POST /runs
GET  /runs/{id}
POST /runs/{id}/cancel
GET  /runs/{id}/logs
GET  /workers
GET  /health
GET  /metrics
```

Responsibilities: validation, auth boundary, idempotency, durable job
creation, cancellation, run state, logs and results. Keep scheduler
logic separate.

## 3. gRPC Worker Protocol

Candidate RPCs:

``` text
RegisterWorker
Heartbeat
LeaseJob
AcknowledgeLease
StreamRunEvents
CompleteRun
FailRun
ReleaseResources
```

Workers advertise CPU, memory, active jobs, sandbox backends,
capabilities, version and heartbeat state. Capabilities may include
`gpu`, `gvisor`, `firecracker`, `high-memory`, and `network-disabled`.

## 4. Scheduler

Build the scheduler directly rather than hiding it behind Kubernetes.

Start with: - worker eligibility - CPU/RAM capacity checks -
best-fit/bin-packing placement

Then add: - priorities and starvation protection - per-user/project
fairness - admission control - capability-aware placement

Only add sharding, batching, partitioned state or caching after
benchmarks demonstrate a real need.

## 5. Leases, Heartbeats and Idempotency

Every dispatched job receives a lease containing job, worker, attempt
and expiry information.

If heartbeats stop: 1. mark worker unhealthy; 2. expire leases; 3.
determine retry safety; 4. requeue safe jobs; 5. record the failure
reason.

Test duplicate API requests, duplicate completions, repeated
acknowledgements, scheduler restarts during dispatch, and client retries
after timeouts.

Be able to explain **at-least-once delivery vs exactly-once effects**.

## 6. Sandboxed Execution

Create a common interface:

``` text
SandboxBackend
  create()
  execute()
  stream_logs()
  stats()
  cancel()
  destroy()
```

### First backend: gVisor

Enforce: - CPU limits - RAM limits - process limits - timeout -
filesystem isolation - temporary workspace - network policy - guaranteed
cleanup

### Optional second backend: Firecracker

Compare cold-start latency, memory overhead, throughput, filesystem
setup and cleanup latency. Keep security-model claims separate from
performance benchmarks.

## 7. Hostile Workload Tests

Test: - memory bomb - fork/process bomb - infinite loop - disk
exhaustion - forbidden network access

Success means the sandboxed job fails safely while the worker host
remains healthy and resources are reclaimed.

## 8. Failure Injection

Make chaos/failure testing first-class.

Required scenarios: - worker killed mid-run - scheduler restart -
database interruption - deliberately slow worker - 10% simulated fleet
loss - fleet at full capacity

Measure detection time, lease expiry, requeue latency, recovery time,
tail latency and job loss. Under overload, demonstrate backpressure
rather than uncontrolled memory growth.

## 9. Observability

Use **OpenTelemetry**.

Trace:

``` text
API
→ persistence
→ queue wait
→ scheduling
→ gRPC dispatch
→ sandbox creation
→ checkout
→ execution
→ tests
→ result
→ cleanup
```

Measure scheduler throughput/latency, queue depth, lease expirations,
worker resources, sandbox failures, heartbeat latency, run duration,
retries, API latency, gRPC latency and DB latency.

Report **p50 / p95 / p99**, not just averages.

## 10. Large-Scale Simulation

Build `agent-fabric-loadgen`.

Simulated workers register, heartbeat, advertise resources, accept jobs,
simulate execution, complete work, fail randomly and recover.

Progressively attempt:

``` text
100
1,000
10,000
100,000
1,000,000 simulated workers
```

**One million is a test target, not a claim.** If V1 collapses at 30k,
profile it and explain why.

## 11. Benchmark Tables

Do not fill values until measured.

  -------------------------------------------------------------------------------
      Workers       Jobs   Scheduling        p50        p95        p99        RAM
                  queued   throughput                                  
  ----------- ---------- ------------ ---------- ---------- ---------- ----------
          100        TBD          TBD        TBD        TBD        TBD        TBD

        1,000        TBD          TBD        TBD        TBD        TBD        TBD

       10,000        TBD          TBD        TBD        TBD        TBD        TBD

      100,000        TBD          TBD        TBD        TBD        TBD        TBD

    1,000,000        TBD          TBD        TBD        TBD        TBD        TBD
  -------------------------------------------------------------------------------

  Failure               Scale   Detection   Recovery   Jobs lost
  ------------------- ------- ----------- ---------- -----------
  Kill worker             TBD         TBD        TBD         TBD
  Kill 10% fleet          TBD         TBD        TBD         TBD
  Scheduler restart       TBD         TBD        TBD         TBD
  DB interruption         TBD         TBD        TBD         TBD

## 12. Find the First Real Bottleneck

The first architecture failing is part of the project.

When degradation appears: 1. identify the scale; 2. show the metric; 3.
profile it; 4. identify the architectural cause; 5. redesign; 6. rerun
the identical benchmark; 7. publish before/after results.

Example hypothesis: synchronously persisting every heartbeat may make
PostgreSQL the bottleneck. If so, separate ephemeral liveness from
durable job state or batch persistence --- but only after measurement
proves the need.

## 13. Terraform Deployment

Deploy only a small real environment:

``` text
network
control plane
database
2–3 real workers
observability
IAM/service identities
```

Terraform should demonstrate modules, environment configuration, IAM
boundaries, networking, service configuration, state strategy, and
reproducible create/destroy.

Do not buy expensive compute for appearances.

## 14. Threat Model

Document:

``` text
User
↓
API
↓
Control Plane
↓
Worker
↓
Sandbox
↓
Untrusted Code
```

Cover trust boundaries, credentials, repository access, network
permissions, secret injection/removal, filesystem destruction, log
redaction and worker-compromise assumptions.

Create `docs/threat-model.md`.

## 15. Testing Strategy

### Unit

Scheduler placement, resource accounting, state transitions, lease
expiry, retries, idempotency, priorities and fairness.

### Integration

API → scheduler → worker, gRPC registration, real sandbox execution,
cancellation, timeout and cleanup.

### Failure

Worker death, scheduler restart, duplicates, DB interruption and
overload.

### Security

Memory/process exhaustion, filesystem isolation, network restriction and
timeout enforcement.

## Suggested Repository Structure

``` text
agent-fabric/
├── README.md
├── Makefile
├── proto/worker.proto
├── control-plane/
│   ├── api/
│   ├── scheduler/
│   ├── state/
│   └── telemetry/
├── worker/
│   ├── runtime/
│   ├── sandbox/
│   │   ├── interface/
│   │   ├── gvisor/
│   │   └── firecracker/
│   └── telemetry/
├── loadgen/
│   ├── simulated_worker/
│   ├── workload/
│   └── chaos/
├── infra/terraform/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── chaos/
│   └── security/
├── benchmarks/
│   ├── scheduler/
│   ├── sandbox/
│   └── recovery/
└── docs/
    ├── architecture.md
    ├── scheduler.md
    ├── sandboxing.md
    ├── threat-model.md
    ├── scaling.md
    └── failures.md
```

## Implementation Roadmap

### Phase 0 --- Design

Architecture, state machine, worker model, lease semantics, protobuf,
threat model and benchmark methodology.

### Phase 1 --- Minimal Execution Plane

FastAPI submission, persistence, one worker, gRPC, scheduler V1,
isolated execution, results and cleanup.

**Exit:** a submitted repository/command runs on an isolated worker and
returns a result.

### Phase 2 --- Reliability

Heartbeats, leases, health, retries, cancellation, idempotency and
restart recovery.

**Exit:** kill a worker mid-run and demonstrate deterministic recovery.

### Phase 3 --- Real Sandboxing

gVisor, resource limits, network controls, filesystem isolation and
hostile workload tests.

**Exit:** hostile workloads are contained without killing the worker
host.

### Phase 4 --- Observability

OpenTelemetry traces, metrics and structured logs.

**Exit:** explain any run's latency from one trace.

### Phase 5 --- Load Generator

Simulated fleet, workloads, failures and benchmark runner.

**Exit:** identify and explain the first real scaling bottleneck.

### Phase 6 --- Optimisation

Profile and redesign the bottleneck.

**Exit:** publish reproducible before/after measurements.

### Phase 7 --- Terraform

Deploy a tiny real environment.

**Exit:** documented reproducible deploy/destroy.

### Phase 8 --- Optional Firecracker

Only after the core project is already strong.

## Suggested 3-Week Schedule

### Week 1 --- Execution

Architecture, protobuf, FastAPI, scheduler V1, worker, gRPC, gVisor and
core tests.

### Week 2 --- Reliability

Leases, heartbeats, idempotency, retries, cancellation, chaos tests,
OpenTelemetry, resource limits and hostile workload tests.

### Week 3 --- Scale & Presentation

Load generator, scaling benchmarks, bottleneck investigation,
optimisation, Terraform, README, diagrams and benchmark report.

If a fourth week is needed, spend it on **Firecracker and deeper
benchmarking**, not UI.

## What Not to Build

Avoid: - elaborate frontend - chat UI - custom LLM - Kubernetes-heavy
architecture that hides the scheduler - unnecessary microservices -
distributed consensus without a measured need - expensive cloud scale -
unsupported hyperscale claims

## README Strategy

Lead with evidence:

1.  What Agent Fabric is
2.  Measured headline results
3.  Architecture
4.  Failure/recovery results
5.  Scaling results
6.  Sandbox benchmarks
7.  First bottleneck and redesign
8.  Quickstart
9.  Threat model
10. Limitations

Do **not** write fake headline numbers. Replace placeholders only after
measurement.

## Definition of Done

-   [ ] FastAPI accepts real execution jobs.
-   [ ] Workers communicate through gRPC.
-   [ ] Scheduler performs resource-aware placement.
-   [ ] Jobs use leases and heartbeats.
-   [ ] Duplicate requests/messages are safe.
-   [ ] Real code executes in gVisor or equivalent isolation.
-   [ ] CPU/RAM/process/time/network controls are tested.
-   [ ] Worker death recovery is demonstrated.
-   [ ] Scheduler restart recovery is demonstrated.
-   [ ] OpenTelemetry traces cover the full run lifecycle.
-   [ ] Load generator can simulate a large worker fleet.
-   [ ] Scaling benchmarks are reproducible.
-   [ ] At least one real bottleneck is profiled and documented.
-   [ ] Terraform deploys a small real environment.
-   [ ] Threat model and limitations are documented.
-   [ ] README leads with measured evidence.

## Potential CV Bullet --- Only After Completion

> Built a sandboxed agent execution control plane with gRPC worker
> orchestration, resource-aware scheduling, lease-based failure
> recovery, OpenTelemetry observability and gVisor isolation;
> load-tested control-plane behaviour against large simulated worker
> fleets and profiled/redesigned the first scaling bottleneck.

Replace "large simulated worker fleets" with actual measured scale and
performance once results exist.
