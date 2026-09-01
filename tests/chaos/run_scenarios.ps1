param(
    [string]$Api = "http://localhost:8000",
    [string]$ApiKey = "af_dev_key"
)

$ErrorActionPreference = "Stop"
$headers = @{ Authorization = "Bearer $ApiKey" }

Write-Host "Restarting scheduler to exercise durable queue recovery"
docker compose restart scheduler
Invoke-RestMethod "$Api/health" | Out-Null

Write-Host "Restarting Redis to exercise outbox/reconciliation recovery"
docker compose restart redis
do {
    Start-Sleep -Seconds 1
    try { $healthy = (Invoke-RestMethod "$Api/health").status -eq "ok" } catch { $healthy = $false }
} until ($healthy)

Write-Host "Chaos smoke scenarios recovered successfully"

