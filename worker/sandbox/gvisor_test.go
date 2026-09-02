package sandbox

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestRejectsLoopbackRepository(t *testing.T) {
	err := validatePublicRepository(context.Background(), "https://127.0.0.1/repository")
	if err == nil {
		t.Fatal("expected loopback repository to be rejected")
	}
}

func TestRejectsCredentialedRepository(t *testing.T) {
	err := validatePublicRepository(context.Background(), "https://user:token@example.com/repository")
	if err == nil {
		t.Fatal("expected credentialed repository to be rejected")
	}
}

func TestCreatesWorkspaceRoot(t *testing.T) {
	root := filepath.Join(t.TempDir(), "nested", "workspaces")
	if err := EnsureWorkspaceRoot(root); err != nil {
		t.Fatalf("create workspace root: %v", err)
	}
}

func TestPurgesStaleWorkspacesOnStart(t *testing.T) {
	root := t.TempDir()
	stale := filepath.Join(root, "agent-fabric-123")
	if err := os.MkdirAll(stale, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stale, "fill.bin"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	keep := filepath.Join(root, "unrelated")
	if err := os.MkdirAll(keep, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := EnsureWorkspaceRoot(root); err != nil {
		t.Fatalf("ensure workspace root: %v", err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatalf("stale workspace survived: %v", err)
	}
	if _, err := os.Stat(keep); err != nil {
		t.Fatalf("unrelated directory removed: %v", err)
	}
}
