.PHONY: generate lint test test-go build up down loadgen

generate:
	docker run --rm -v "$$(pwd):/workspace" -w /workspace bufbuild/buf:1.47.2 generate
	python scripts/fix_generated.py

lint:
	docker build -f control-plane/Dockerfile --target runtime -t agent-fabric-control .
	docker compose config --quiet

test:
	docker build -f control-plane/Dockerfile --target test -t agent-fabric-test .
	docker run --rm agent-fabric-test

test-go:
	docker run --rm -v "$$(pwd)/worker:/src" -w /src golang:1.24-bookworm sh -c "/usr/local/go/bin/go test ./..."

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

loadgen:
	docker build -f loadgen/Dockerfile -t agent-fabric-loadgen .
	docker run --rm --network agent-fabric_default agent-fabric-loadgen --control grpc:50051 --api http://api:8000 --workers 100 --jobs 1000 --duration 60
