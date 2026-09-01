# Sandboxing

The production backend launches OCI images through Docker's `runsc` runtime. Root filesystems are read-only, Linux capabilities are dropped, privilege escalation is disabled, the workload runs as a non-root UID, and only a per-run workspace is writable. CPU, memory, PID, timeout, and network policies are passed explicitly.

Repository fetching is separate from execution. Both the API and worker reject credentials and literal private addresses; the worker resolves the hostname immediately before fetch and rejects non-public results. Git hooks, submodules, LFS, interactive credentials, system configuration, and the file protocol are disabled.

`disabled` networking uses `--network=none`. `open` networking means ordinary outbound bridge access and should be treated as a material risk. No domain-restricted claim is made.

The worker must run on Linux with a quota-enabled workspace filesystem before disk limits are described as hard enforcement. Cleanup always attempts forced container removal and workspace deletion.

