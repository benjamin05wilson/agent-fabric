# Reproducible development

The readiness baseline uses **Python 3.12.13**, **uv 0.12.0**, **Go 1.24.13** and
**Buf 1.47.2**. Python's supported range is deliberately 3.12; `.python-version`,
Docker and CI agree. `uv.lock` records runtime, dev and optional chart dependencies
with artifact hashes. Do not install broad ranges afresh with `pip install .`
when reproducing results.

```bash
# uv must already be installed; downloads and caches stay inside this checkout.
export UV_CACHE_DIR="$PWD/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.cache/python"
uv python install 3.12.13
uv sync --locked --extra dev
uv run --locked --extra dev pytest -q
uv run --locked --extra dev ruff check control-plane tests migrations
uv run --locked --extra dev mypy control-plane/agent_fabric
```

PowerShell equivalents for repository-local storage:

```powershell
$env:UV_CACHE_DIR = "$PWD/.cache/uv"
$env:UV_PYTHON_INSTALL_DIR = "$PWD/.cache/python"
uv python install 3.12.13
uv sync --locked --extra dev
uv run --locked --extra dev pytest -q
```

Docker uses uv 0.12.0 and `uv sync --frozen` against the same lock; CI first runs
`uv sync --locked` to reject stale project metadata. The test image adds the locked
dev extra. Hatchling is pinned in the build-system requirements. Runtime images
use the Python patch tag; OS image tags are not immutable digest snapshots.

`protobuf>=6.31.1,<7` is declared explicitly because checked-in bindings now come
from protoc 31.1 (Python gencode 6.31.1). `grpcio-tools==1.74.0` is compatible with
that protobuf line. Python, pyi, Go and gRPC remote generators have explicit
versions **and revisions** in `buf.gen.yaml`. The Go module name matches this public repository. The proto `go_package`
retains the historical `github.com/example/...` option: changing it failed Buf
compatibility against main, so it is deliberately preserved for generated clients.
The breaking-change check is unchanged.

```bash
# Buf 1.47.2 on PATH, or use `make generate` with Docker.
export BUF_CACHE_DIR="$PWD/.cache/buf"
buf generate
uv run --locked --extra dev python scripts/fix_generated.py
git diff --exit-code -- control-plane/agent_fabric/generated worker/gen

# Go 1.24.13 on PATH; keep dependencies/build cache in this checkout.
export GOPATH="$PWD/.cache/gopath" GOCACHE="$PWD/.cache/go-build"
go -C worker test -race ./...
go -C worker vet ./...
```

Regeneration was checked locally twice, byte-for-byte, after the deliberate
protoc downgrade and module-identity change. CI regenerates and fails on any diff.
Normal Go tests skip the explicitly enabled runsc integration test; normal Python
tests skip the service suite. Run [the small Compose gate](demo.md) to exercise
PostgreSQL/Redis and [the Linux fixture](real-worker.md) for runsc.

To update dependencies, change the declared requirements, run `uv lock`, then run
these checks and the small integration gate. To regenerate the historical chart:

```bash
uv sync --locked --extra dev --extra evidence
uv run --locked --extra evidence python scripts/evidence_chart.py
```

References: [uv locking/sync semantics](https://docs.astral.sh/uv/concepts/projects/sync/)
and [Buf version/revision pinning](https://buf.build/docs/configuration/v2/buf-gen-yaml/).
