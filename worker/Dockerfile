FROM golang:1.24-bookworm AS build
WORKDIR /src
COPY worker/go.mod worker/go.sum ./
RUN /usr/local/go/bin/go mod download
COPY worker .
RUN CGO_ENABLED=0 /usr/local/go/bin/go build -trimpath -ldflags="-s -w" -o /out/agent-fabric-worker .

FROM docker:29-cli
RUN apk add --no-cache ca-certificates git
COPY --from=build /out/agent-fabric-worker /usr/local/bin/agent-fabric-worker
ENTRYPOINT ["agent-fabric-worker"]

