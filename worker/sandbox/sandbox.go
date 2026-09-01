package sandbox

import (
	"context"
	"io"
)

type Spec struct {
	RunID          string
	AttemptID      string
	RepositoryURL  string
	RepositoryRef  string
	Argv           []string
	Environment    map[string]string
	Image          string
	CPUMillis      int64
	MemoryMB       int64
	PIDs           int64
	DiskMB         int64
	TimeoutSeconds int64
	NetworkPolicy  string
}

type Event struct {
	Stream string
	Data   []byte
}

type Result struct {
	ExitCode   int
	State      string
	ReasonCode string
	Message    string
}

type Backend interface {
	Preflight(context.Context) error
	Execute(context.Context, Spec, chan<- Event) Result
	Cancel(context.Context, string) error
	Destroy(context.Context, string) error
}

func StreamLines(reader io.Reader, stream string, events chan<- Event) {
	buffer := make([]byte, 32*1024)
	for {
		n, err := reader.Read(buffer)
		if n > 0 {
			data := append([]byte(nil), buffer[:n]...)
			events <- Event{Stream: stream, Data: data}
		}
		if err != nil {
			return
		}
	}
}
