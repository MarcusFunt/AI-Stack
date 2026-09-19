param(
    [switch]$StatusOnly,
    [switch]$DisablePublicMcp
)

$ErrorActionPreference = "Stop"
$status = tailscale status --json | ConvertFrom-Json
$dnsName = $status.Self.DNSName.TrimEnd(".")

if (-not $StatusOnly) {
    tailscale serve --https=8443 --bg --yes 3000 | Out-Null
    if ($DisablePublicMcp) {
        tailscale funnel --https=10000 off | Out-Null
    } else {
        tailscale funnel --https=10000 --bg --yes 8765 | Out-Null
    }
}

Write-Host ("Private Local AI: https://" + $dnsName + ":8443/")
if (-not $DisablePublicMcp) {
    Write-Host ("Public MCP (Bearer): https://" + $dnsName + ":10000/mcp")
}
Write-Host ""
tailscale serve status
