package sandbox

import (
	"context"
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
