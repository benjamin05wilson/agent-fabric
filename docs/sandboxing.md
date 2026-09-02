# Sandboxing

The production backend launches OCI images through Docker's `runsc` runtime. Root filesystems are read-only, Linux capabilities are dropped, privilege escalation is disabled, the workload runs as a non-root UID, and only a per-run workspace is writable. CPU, memory, PID, timeout, and network policies are passed explicitly.

Repository fetching is separate from execution. Both the API and worker reject credentials and literal private addresses; the worker resolves the hostname immediately before fetch and rejects non-public results. Git hooks, submodules, LFS, interactive credentials, system configuration, and the file protocol are disabled.

The PID limit handed to Docker is the lease's budget plus a fixed overhead for gVisor's own Sentry and gofer threads, which live in the same pids cgroup: measured on the benchmark host, `--pids-limit=16` made every sandbox fail to start (exit 125, "waiting for sandbox to start: EOF") while 32 and 64 worked, so the worker adds 48 and reports a Docker exit status of 125 as `SANDBOX_START` rather than as the workload's exit code. When a workload does exhaust the limit, gVisor terminates the whole sandbox (exit status 2, no output) instead of failing `fork()` with `EAGAIN` as runc does; the run is still contained, but the workload gets no chance to report.

`disabled` networking uses `--network=none`. `open` networking means ordinary outbound bridge access and should be treated as a material risk. No domain-restricted claim is made.

The worker must run on Linux with a quota-enabled workspace filesystem before disk limits are described as hard enforcement. Cleanup always attempts forced container removal and workspace deletion.

