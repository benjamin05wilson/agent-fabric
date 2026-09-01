param(
    [int[]]$Tiers = @(100, 1000, 10000),
    [int]$Jobs = 10000,
    [int]$Duration = 120
)

$ErrorActionPreference = "Stop"
foreach ($tier in $Tiers) {
    $memory = docker info --format '{{.MemTotal}}'
    if (-not $memory) { throw "Docker memory limit could not be read" }
    $output = "benchmarks/results/workers-$tier.json"
    docker compose --profile load run --rm loadgen `
        --control grpc:50051 --api http://api:8000 `
        --workers $tier --jobs $Jobs --duration $Duration --register-timeout 120 `
        --output "/results/workers-$tier.json"
    Write-Host "Completed $tier-worker tier; report written to $output."
}
