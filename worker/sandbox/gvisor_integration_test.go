package sandbox

import (
	"context"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"testing"
	"time"
)

// Native Linux only: WORKSPACE_ROOT and the Docker daemon must share a filesystem.
// This is intentionally opt-in, never part of scale/hostile tests.
func TestNativeRunscReadsCheckoutAndCleans(t *testing.T) {
	if os.Getenv("AF_RUNSC_TEST") != "1" {
		t.Skip("set AF_RUNSC_TEST=1 on a native Linux host with runsc and python:3.12-slim prepared")
	}
	if runtime.GOOS != "linux" {
		t.Fatal("native Linux required; a desktop/remote daemon does not share TempDir paths")
	}
	root := t.TempDir()
	g := NewGVisor(root)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	if err := g.Preflight(ctx); err != nil {
		t.Fatal(err)
	}
	events := make(chan Event, 100)
	attempt := "fixture-" + time.Now().Format("150405000000000")
	result := g.Execute(ctx, Spec{
		RunID: attempt, AttemptID: attempt,
		RepositoryURL: "https://github.com/octocat/Hello-World", RepositoryRef: "master",
		Image: "python:3.12-slim", CPUMillis: 1000, MemoryMB: 128, PIDs: 16,
		DiskMB: 128, TimeoutSeconds: 20, NetworkPolicy: "disabled",
		Argv: []string{"python", "-c", "from pathlib import Path; s=Path('README').read_text().strip(); assert s == 'Hello World!'; print('fixture: '+s)"},
	}, events)
	close(events)
	var output strings.Builder
	for event := range events {
		if event.Stream == "stdout" {
			output.Write(event.Data)
		}
	}
	if result.State != "SUCCEEDED" || result.ExitCode != 0 {
		t.Fatalf("sandbox: %+v; output: %s", result, output.String())
	}
	if !strings.Contains(output.String(), "fixture: Hello World!") {
		t.Fatalf("checkout not read: %q", output.String())
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 0 {
		t.Fatalf("checkout survived: %v %v", entries, err)
	}
	if err := exec.CommandContext(ctx, "docker", "inspect", "af-"+strings.ReplaceAll(attempt, "-", "")).Run(); err == nil {
		t.Fatal("sandbox container survived Execute")
	}
	t.Logf("%s; terminal=%s; checkout/container removed", strings.TrimSpace(output.String()), result.State)
}
