# Real worker: native Linux topology (unverified on the readiness host)

The first demo is simulated. **Compose real-worker execution remains unverified
until a Linux/runsc host passes this fixture.** The readiness host is macOS without
Docker. The source-derived mount mismatch has been repaired: the worker now uses
an explicit bind at the same absolute host/container path, with implicit path
creation disabled. The previous named volume did not make a sibling sandbox's
host bind path point at the checkout.

Use a native Linux Docker Engine on the **same host/filesystem as the Go worker**,
with runsc already installed and registered. No Docker context changes or daemon
restarts are performed by these instructions. A remote daemon or container-only
workspace path is not this topology. See [gVisor's Docker guide](https://gvisor.dev/docs/user_guide/quick_start/docker/)
for runtime preparation. Downloads, image pulls and compilation are preparation.

```bash
# All paths below belong to this checkout. Run from repository root.
export AF_WORKSPACE_ROOT="$PWD/.cache/workspaces"
mkdir -p "$AF_WORKSPACE_ROOT" .cache/bin
export GOPATH="$PWD/.cache/gopath" GOCACHE="$PWD/.cache/go-build"
go -C worker build -o "$PWD/.cache/bin/agent-fabric-worker" .
docker info --format '{{json .Runtimes}}'  # must include runsc
docker pull python:3.12-slim

# Start the full local control plane on loopback 8000 and 50051. These ports must be free.
docker compose -p agent-fabric-native-fixture up --build -d --wait
```

In a second terminal, with the same absolute workspace path, run the **native** worker:

```bash
.cache/bin/agent-fabric-worker --control 127.0.0.1:50051 \
  --workspace-root "$PWD/.cache/workspaces" --worker-id native-fixture
```

Then submit exactly one standard-library-only job, with a bounded poll:

```bash
python3 scripts/real_fixture.py
```

It clones the public `octocat/Hello-World` `master` branch and reads its checked-in
`README`; the sandbox must print `fixture: Hello World!` and durably reach
`SUCCEEDED`. Missing/empty/misbound workspaces cannot pass. This public fixture's
branch and Python image are mutable upstream inputs; capture the fetched ref/image
digest before describing a run as immutable reproduction. No pytest installation
inside the workload is needed. No hostile workload is involved.

An opt-in backend regression independently checks the same file, terminal result,
checkout removal and container removal under a 120 s deadline:

```bash
AF_RUNSC_TEST=1 go -C worker test -v -timeout 150s ./sandbox -run '^TestNativeRunscReadsCheckoutAndCleans$'
```

For a containerized worker on that same Linux host, stop the native worker first,
then use the repaired binding:

```bash
docker compose -p agent-fabric-native-fixture --profile gvisor up --build worker
```

`AF_WORKSPACE_ROOT` must name an existing absolute host directory. The worker and
sandbox daemon see the same path. This alternative also remains runtime-unverified
here; Docker Desktop path sharing is not asserted by the native-host instructions.

After the test, stop the worker with Ctrl-C. Its own deferred cleanup should remove
all `agent-fabric-*` checkouts and `af-*` sandbox containers. Inspect them and retain
the fixture JSON/Go transcript, then remove only this control-plane project:

```bash
docker compose -p agent-fabric-native-fixture down --volumes --remove-orphans
```

A successful backend fixture does not certify disk quotas, authenticated fleet
transport, production readiness, or exactly-once external side effects. Those
limits remain in [sandboxing](sandboxing.md) and the historical failure reports.
