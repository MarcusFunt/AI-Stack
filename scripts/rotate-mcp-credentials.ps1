$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root ".env"

function New-Secret {
    $bytes = New-Object byte[] 32
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return (([BitConverter]::ToString($bytes) -replace "-", "").ToLower())
}

$lines = Get-Content $envPath | Where-Object {
    $_ -notmatch "^MCP_API_KEY=" -and $_ -notmatch "^MCP_URL_TOKEN="
}
$lines | Set-Content $envPath
Add-Content $envPath ("MCP_API_KEY=" + (New-Secret))
Add-Content $envPath ("MCP_URL_TOKEN=" + (New-Secret))

Push-Location $root
try {
    docker compose up -d --force-recreate mcp
} finally {
    Pop-Location
}

Write-Host "MCP credentials rotated and the MCP container was recreated."
Write-Host "Run .\scripts\show-mcp-connection.ps1 to view the new connection details."
