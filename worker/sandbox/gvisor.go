package sandbox

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// gVisor's Sentry and gofer run inside the container's pids cgroup, so the limit passed to
// Docker must leave room for them: with --pids-limit=16 every sandbox failed to start
// ("waiting for sandbox to start: EOF", exit 125) while 32 and 64 worked on the benchmark
// host. The worker adds this measured overhead to the lease's PID budget so the number the
// workload sees is the number the API promised.
const gvisorPIDOverhead = 48

type GVisor struct {
	WorkspaceRoot string
	mu            sync.Mutex
	containers    map[string]string
}

func NewGVisor(workspaceRoot string) *GVisor {
	return &GVisor{WorkspaceRoot: workspaceRoot, containers: make(map[string]string)}
}

func (g *GVisor) Preflight(ctx context.Context) error {
	output, err := exec.CommandContext(ctx, "docker", "info", "--format", "{{json .Runtimes}}").Output()
	if err != nil {
		return fmt.Errorf("docker preflight: %w", err)
	}
	var runtimes map[string]any
	if err := json.Unmarshal(output, &runtimes); err != nil {
		return fmt.Errorf("decode Docker runtimes: %w", err)
	}
	if _, ok := runtimes["runsc"]; !ok {
		return errors.New("Docker runtime 'runsc' is not installed")
	}
	return nil
}

func (g *GVisor) Execute(parent context.Context, spec Spec, events chan<- Event) Result {
	if err := validatePublicRepository(parent, spec.RepositoryURL); err != nil {
		return Result{ExitCode: -1, State: "FAILED", ReasonCode: "INVALID_REPOSITORY", Message: err.Error()}
	}
	workspace, err := os.MkdirTemp(g.WorkspaceRoot, "agent-fabric-")
	if err != nil {
		return Result{ExitCode: -1, State: "FAILED", ReasonCode: "WORKSPACE_CREATE", Message: err.Error()}
	}
	defer os.RemoveAll(workspace)
	if err := cloneRepository(parent, spec, workspace, events); err != nil {
		return Result{ExitCode: -1, State: "FAILED", ReasonCode: "REPOSITORY_FETCH", Message: err.Error()}
	}
	if err := os.Chmod(workspace, 0o777); err != nil {
		return Result{ExitCode: -1, State: "FAILED", ReasonCode: "WORKSPACE_PERMISSIONS", Message: err.Error()}
	}

	runCtx, cancel := context.WithTimeout(parent, time.Duration(spec.TimeoutSeconds)*time.Second)
	defer cancel()
	name := "af-" + strings.ToLower(strings.ReplaceAll(spec.AttemptID, "-", ""))
	g.mu.Lock()
	g.containers[spec.AttemptID] = name
	g.mu.Unlock()
	defer func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cleanupCancel()
		_ = g.Destroy(cleanupCtx, spec.AttemptID)
	}()

	args := []string{
		"run", "--rm", "--name", name, "--runtime=runsc",
		"--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
		"--user=65532:65532",
		"--cpus=" + strconv.FormatFloat(float64(spec.CPUMillis)/1000, 'f', 3, 64),
		"--memory=" + strconv.FormatInt(spec.MemoryMB, 10) + "m",
		"--pids-limit=" + strconv.FormatInt(spec.PIDs+gvisorPIDOverhead, 10),
		"--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
		"--mount=type=bind,src=" + workspace + ",dst=/workspace",
		"--workdir=/workspace",
	}
	if spec.NetworkPolicy == "disabled" {
		args = append(args, "--network=none")
	}
	if spec.GPUCount > 0 {
		args = append(args, "--gpus="+strconv.FormatInt(spec.GPUCount, 10))
	}
	for key, value := range spec.Environment {
		args = append(args, "--env", key+"="+value)
	}
	args = append(args, spec.Image)
	args = append(args, spec.Argv...)

	cmd := exec.CommandContext(runCtx, "docker", args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return Result{ExitCode: -1, State: "FAILED", ReasonCode: "SANDBOX_START", Message: err.Error()}
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return Result{ExitCode: -1, State: "FAILED", ReasonCode: "SANDBOX_START", Message: err.Error()}
	}
	if err := cmd.Start(); err != nil {
		return Result{ExitCode: -1, State: "FAILED", ReasonCode: "SANDBOX_START", Message: err.Error()}
	}
	var streams sync.WaitGroup
	streams.Add(2)
	go func() { defer streams.Done(); StreamLines(stdout, "stdout", events) }()
	go func() { defer streams.Done(); StreamLines(stderr, "stderr", events) }()
	err = cmd.Wait()
	streams.Wait()
	if errors.Is(runCtx.Err(), context.DeadlineExceeded) {
		return Result{ExitCode: -1, State: "TIMED_OUT", ReasonCode: "TIMEOUT", Message: "wall-clock timeout exceeded"}
	}
	if errors.Is(runCtx.Err(), context.Canceled) || errors.Is(parent.Err(), context.Canceled) {
		return Result{ExitCode: -1, State: "CANCELLED", ReasonCode: "CANCELLED", Message: "cancelled by control plane"}
	}
	if err == nil {
		return Result{ExitCode: 0, State: "SUCCEEDED"}
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		if exitErr.ExitCode() == 125 {
			// Docker's own exit status: the daemon or runtime refused to start the
			// container, so the workload never ran. Report it as a sandbox failure, not
			// as the workload's exit code.
			return Result{ExitCode: exitErr.ExitCode(), State: "FAILED", ReasonCode: "SANDBOX_START", Message: err.Error()}
		}
		return Result{ExitCode: exitErr.ExitCode(), State: "FAILED", ReasonCode: "NON_ZERO_EXIT", Message: err.Error()}
	}
	return Result{ExitCode: -1, State: "FAILED", ReasonCode: "EXECUTION_ERROR", Message: err.Error()}
}

func (g *GVisor) Cancel(ctx context.Context, attemptID string) error {
	g.mu.Lock()
	name := g.containers[attemptID]
	g.mu.Unlock()
	if name == "" {
		return nil
	}
	grace := exec.CommandContext(ctx, "docker", "stop", "--time=5", name)
	if err := grace.Run(); err == nil {
		return nil
	}
	return exec.CommandContext(ctx, "docker", "kill", name).Run()
}

func (g *GVisor) Destroy(ctx context.Context, attemptID string) error {
	g.mu.Lock()
	name := g.containers[attemptID]
	delete(g.containers, attemptID)
	g.mu.Unlock()
	if name == "" {
		return nil
	}
	output, err := exec.CommandContext(ctx, "docker", "rm", "--force", "--volumes", name).CombinedOutput()
	if err != nil && !strings.Contains(string(output), "No such container") {
		return fmt.Errorf("remove sandbox: %s: %w", strings.TrimSpace(string(output)), err)
	}
	return nil
}

func validatePublicRepository(ctx context.Context, raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil {
		return errors.New("repository must be a credential-free HTTPS URL")
	}
	addresses, err := net.DefaultResolver.LookupIP(ctx, "ip", parsed.Hostname())
	if err != nil {
		return fmt.Errorf("resolve repository host: %w", err)
	}
	if len(addresses) == 0 {
		return errors.New("repository host has no addresses")
	}
	for _, address := range addresses {
		if !address.IsGlobalUnicast() || address.IsPrivate() || address.IsLoopback() || address.IsLinkLocalUnicast() {
			return fmt.Errorf("repository host resolved to non-public address %s", address)
		}
	}
	return nil
}

func cloneRepository(ctx context.Context, spec Spec, workspace string, events chan<- Event) error {
	cloneCtx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	args := []string{
		"-c", "core.hooksPath=/dev/null",
		"-c", "protocol.file.allow=never",
		"clone", "--depth=1", "--no-tags", "--no-recurse-submodules",
	}
	if spec.RepositoryRef != "HEAD" {
		args = append(args, "--branch", spec.RepositoryRef)
	}
	args = append(args, "--", spec.RepositoryURL, workspace)
	cmd := exec.CommandContext(cloneCtx, "git", args...)
	cmd.Env = append(os.Environ(), "GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0")
	output, err := cmd.CombinedOutput()
	if len(output) > 0 {
		events <- Event{Stream: "system", Data: output}
	}
	if err != nil {
		return fmt.Errorf("git clone: %w", err)
	}
	return nil
}

func EnsureWorkspaceRoot(path string) error {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(absolute, 0o700); err != nil {
		return err
	}
	return purgeStaleWorkspaces(absolute)
}

// purgeStaleWorkspaces removes per-attempt directories left behind by a previous worker
// process that exited without running its deferred cleanup. A hostile disk-exhaustion
// run measured a 28 GB workspace surviving a worker crash until the host was manually
// cleaned; every directory here belongs to an attempt whose lease has long expired.
func purgeStaleWorkspaces(root string) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() && strings.HasPrefix(entry.Name(), "agent-fabric-") {
			if err := os.RemoveAll(filepath.Join(root, entry.Name())); err != nil {
				return fmt.Errorf("purge stale workspace %s: %w", entry.Name(), err)
			}
		}
	}
	return nil
}
