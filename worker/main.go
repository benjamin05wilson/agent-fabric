package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	workerv1 "github.com/bvwilson/agent-fabric/worker/gen"
	"github.com/bvwilson/agent-fabric/worker/sandbox"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type activeRun struct {
	cancel context.CancelFunc
}

type worker struct {
	id       string
	backend  sandbox.Backend
	stream   grpc.BidiStreamingClient[workerv1.WorkerMessage, workerv1.ControlMessage]
	outgoing chan *workerv1.WorkerMessage
	mu       sync.Mutex
	active   map[string]activeRun
}

func main() {
	controlAddress := flag.String("control", envOr("CONTROL_PLANE_GRPC", "localhost:50051"), "gRPC control-plane address")
	workspaceRoot := flag.String("workspace-root", envOr("WORKSPACE_ROOT", "/var/lib/agent-fabric/workspaces"), "workspace root")
	workerID := flag.String("worker-id", envOr("WORKER_ID", defaultWorkerID()), "stable worker identifier")
	allowUnsafe := flag.Bool("allow-missing-runsc", false, "register without a successful runsc preflight (tests only)")
	gpuCount := flag.Int64("gpu-count", envInt64("WORKER_GPU_COUNT", 0), "GPUs offered by this worker")
	vramMB := flag.Int64("vram-mb", envInt64("WORKER_VRAM_MB", 0), "aggregate GPU VRAM offered in MiB")
	flag.Parse()
	capabilities := envList("WORKER_CAPABILITIES", []string{"network-disabled"})
	if *gpuCount > 0 && !contains(capabilities, "cuda") {
		capabilities = append(capabilities, "cuda")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := sandbox.EnsureWorkspaceRoot(*workspaceRoot); err != nil {
		slog.Error("create workspace root", "error", err)
		os.Exit(1)
	}
	backend := sandbox.NewGVisor(*workspaceRoot)
	if err := backend.Preflight(ctx); err != nil && !*allowUnsafe {
		slog.Error("gVisor preflight failed", "error", err)
		os.Exit(1)
	}
	if err := run(ctx, *controlAddress, *workerID, backend, *gpuCount, *vramMB, capabilities); err != nil && ctx.Err() == nil {
		slog.Error("worker stopped", "error", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, address, workerID string, backend sandbox.Backend, gpuCount, vramMB int64, capabilities []string) error {
	connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return err
	}
	defer connection.Close()
	stream, err := workerv1.NewWorkerControlClient(connection).Connect(ctx)
	if err != nil {
		return err
	}
	w := &worker{id: workerID, backend: backend, stream: stream, outgoing: make(chan *workerv1.WorkerMessage, 1000), active: make(map[string]activeRun)}
	errCh := make(chan error, 2)
	go func() { errCh <- w.sendLoop(ctx) }()
	go func() { errCh <- w.receiveLoop(ctx) }()
	w.outgoing <- &workerv1.WorkerMessage{
		WorkerId: workerID,
		Payload: &workerv1.WorkerMessage_Register{Register: &workerv1.Register{
			ProtocolVersion: "v1", WorkerVersion: "0.1.0",
			CpuMillis: int64(runtime.NumCPU() * 1000), MemoryMb: 16 * 1024, Pids: 4096,
			Capabilities: capabilities, SandboxBackends: []string{"gvisor"}, GpuCount: gpuCount, VramMb: vramMB,
		}},
	}
	go w.heartbeatLoop(ctx)
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-errCh:
		return err
	}
}

func (w *worker) sendLoop(ctx context.Context) error {
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case message := <-w.outgoing:
			if err := w.stream.Send(message); err != nil {
				return err
			}
		}
	}
}

func (w *worker) receiveLoop(ctx context.Context) error {
	for {
		message, err := w.stream.Recv()
		if err != nil {
			return err
		}
		switch payload := message.Payload.(type) {
		case *workerv1.ControlMessage_Lease:
			go w.execute(ctx, payload.Lease)
		case *workerv1.ControlMessage_Cancel:
			w.cancel(payload.Cancel.AttemptId)
		case *workerv1.ControlMessage_Drain:
			slog.Info("worker draining", "reason", payload.Drain.Reason)
		case *workerv1.ControlMessage_Error:
			slog.Error("control protocol error", "code", payload.Error.Code, "message", payload.Error.Message)
		}
	}
}

func (w *worker) heartbeatLoop(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			w.mu.Lock()
			ids := make([]string, 0, len(w.active))
			for id := range w.active {
				ids = append(ids, id)
			}
			w.mu.Unlock()
			sort.Strings(ids)
			w.outgoing <- &workerv1.WorkerMessage{WorkerId: w.id, Payload: &workerv1.WorkerMessage_Heartbeat{Heartbeat: &workerv1.Heartbeat{UnixMillis: now.UnixMilli(), ActiveAttemptIds: ids}}}
		}
	}
}

func (w *worker) execute(parent context.Context, lease *workerv1.LeaseOffer) {
	if lease.ExpiresUnixMillis > 0 && lease.ExpiresUnixMillis < time.Now().UnixMilli() {
		// The control plane has already reclaimed this offer; running it would execute the
		// run twice once it is leased elsewhere.
		slog.Warn("lease expired before receipt", "attempt_id", lease.AttemptId)
		w.outgoing <- &workerv1.WorkerMessage{WorkerId: w.id, Payload: &workerv1.WorkerMessage_Acknowledgement{Acknowledgement: &workerv1.LeaseAcknowledgement{RunId: lease.RunId, AttemptId: lease.AttemptId, LeaseToken: lease.LeaseToken, Accepted: false, Reason: "lease expired before receipt"}}}
		return
	}
	ctx, cancel := context.WithCancel(parent)
	w.mu.Lock()
	if _, exists := w.active[lease.AttemptId]; exists {
		w.mu.Unlock()
		cancel()
		return
	}
	w.active[lease.AttemptId] = activeRun{cancel: cancel}
	w.mu.Unlock()
	defer func() { w.mu.Lock(); delete(w.active, lease.AttemptId); w.mu.Unlock(); cancel() }()
	w.outgoing <- &workerv1.WorkerMessage{WorkerId: w.id, Payload: &workerv1.WorkerMessage_Acknowledgement{Acknowledgement: &workerv1.LeaseAcknowledgement{RunId: lease.RunId, AttemptId: lease.AttemptId, LeaseToken: lease.LeaseToken, Accepted: true}}}
	events := make(chan sandbox.Event, 100)
	done := make(chan sandbox.Result, 1)
	go func() {
		done <- w.backend.Execute(ctx, sandbox.Spec{RunID: lease.RunId, AttemptID: lease.AttemptId, RepositoryURL: lease.RepositoryUrl, RepositoryRef: lease.RepositoryRef, Argv: lease.Argv, Environment: lease.Environment, Image: lease.ImageDigest, CPUMillis: lease.CpuMillis, MemoryMB: lease.MemoryMb, PIDs: lease.Pids, GPUCount: lease.GpuCount, VRAMMB: lease.VramMb, DiskMB: lease.DiskMb, TimeoutSeconds: lease.TimeoutSeconds, NetworkPolicy: lease.NetworkPolicy}, events)
		close(events)
	}()
	sequence := uint64(0)
	for event := range events {
		sequence++
		w.outgoing <- &workerv1.WorkerMessage{WorkerId: w.id, Payload: &workerv1.WorkerMessage_Event{Event: &workerv1.RunEvent{RunId: lease.RunId, AttemptId: lease.AttemptId, Sequence: sequence, Stream: event.Stream, Data: event.Data, UnixMillis: time.Now().UnixMilli()}}}
	}
	result := <-done
	w.outgoing <- &workerv1.WorkerMessage{WorkerId: w.id, Payload: &workerv1.WorkerMessage_Completion{Completion: &workerv1.RunCompletion{RunId: lease.RunId, AttemptId: lease.AttemptId, LeaseToken: lease.LeaseToken, ExitCode: int32(result.ExitCode), TerminalState: result.State, ReasonCode: result.ReasonCode, Message: result.Message}}}
	cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 15*time.Second)
	err := w.backend.Destroy(cleanupCtx, lease.AttemptId)
	cleanupCancel()
	message := ""
	if err != nil {
		message = err.Error()
	}
	w.outgoing <- &workerv1.WorkerMessage{WorkerId: w.id, Payload: &workerv1.WorkerMessage_Cleanup{Cleanup: &workerv1.CleanupConfirmation{RunId: lease.RunId, AttemptId: lease.AttemptId, Successful: err == nil, Message: message}}}
}

func (w *worker) cancel(attemptID string) {
	w.mu.Lock()
	run, exists := w.active[attemptID]
	w.mu.Unlock()
	if exists {
		run.cancel()
	}
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt64(key string, fallback int64) int64 {
	value := os.Getenv(key)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil || parsed < 0 {
		slog.Error("invalid integer environment value", "key", key, "value", value)
		os.Exit(2)
	}
	return parsed
}

func envList(key string, fallback []string) []string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	result := []string{}
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			result = append(result, item)
		}
	}
	return result
}

func contains(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}

func defaultWorkerID() string {
	hostname, _ := os.Hostname()
	random := make([]byte, 4)
	_, _ = rand.Read(random)
	return fmt.Sprintf("%s-%s", hostname, hex.EncodeToString(random))
}
