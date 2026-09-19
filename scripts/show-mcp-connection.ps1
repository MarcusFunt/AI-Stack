$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root ".env"
$lines = Get-Content $envPath

function Read-EnvValue([string]$name) {
    $line = $lines | Where-Object { $_ -match ("^" + [regex]::Escape($name) + "=") } | Select-Object -Last 1
    if (-not $line) { throw "$name is not configured" }
    return ($line -split "=", 2)[1]
}

$bearer = Read-EnvValue "MCP_API_KEY"
$urlToken = Read-EnvValue "MCP_URL_TOKEN"
$status = tailscale status --json | ConvertFrom-Json
$dnsName = $status.Self.DNSName.TrimEnd(".")

Write-Host "Bearer endpoint:"
Write-Host ("  https://" + $dnsName + ":10000/mcp")
Write-Host "Authorization header:"
Write-Host ("  Bearer " + $bearer)
Write-Host ""
Write-Host "No-header capability URL (treat this entire URL as a secret):"
Write-Host ("  https://" + $dnsName + ":10000/" + $urlToken + "/mcp")
