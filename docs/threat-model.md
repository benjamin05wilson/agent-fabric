# Threat model

## Trust boundaries

```text
Authenticated project -> API -> control plane -> worker daemon -> gVisor -> untrusted code
```

The API, PostgreSQL, Redis, MinIO, scheduler, and worker daemon are trusted. Repository content and executed commands are untrusted. gVisor reduces kernel attack surface but is not treated as an absolute security boundary. A worker compromise is assumed to expose that worker and any active workspaces; control-plane credentials must therefore be scoped and workers must not hold cloud-wide credentials.

## Controls

- API keys are stored as SHA-256 digests and compared in constant time.
- Repository URLs cannot contain credentials and are checked again after DNS resolution.
- V1 has no secret injection, private repository access, submodules, or arbitrary images.
- Profile images are controlled by the operator and should be pinned by digest.
- Workloads receive no Docker socket, host devices, Linux capabilities, or writable host paths beyond their workspace.
- Network-disabled jobs cannot establish runtime network connections.
- Authorization headers, URL user information, and future secret fields must be redacted from telemetry.
- Logs are bounded and project-scoped at read time.

## Residual risks

- Open-network jobs can exfiltrate public workspace contents or attack remote services.
- A gVisor escape or compromised Docker daemon compromises the worker host.
- DNS rebinding between validation and Git's connection remains possible without a fetch proxy that pins resolved addresses.
- Dependency images and public repositories are supply-chain inputs; digest pinning and provenance verification remain operator duties.
- Static API keys do not provide user-level attribution or rotation workflows.

