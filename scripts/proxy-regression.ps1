#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Assert-GatewayProxy {
  $health = Invoke-RestMethod "http://127.0.0.1:3000/api/health" -TimeoutSec 10
  if($health.service -ne "gateway") {
    throw "dashboard /api is not routed to gateway: $($health | ConvertTo-Json -Compress)"
  }
  $models = Invoke-RestMethod "http://127.0.0.1:3000/api/v1/models" -TimeoutSec 10
  if(-not $models.data -or @($models.data).Count -lt 1) {
    throw "dashboard proxy model discovery failed"
  }

  $agentLab = Invoke-RestMethod "http://127.0.0.1:3000/agent-lab-api/health" -TimeoutSec 10
  if($agentLab.service -ne "agent-lab" -or $agentLab.status -notin @("ok","degraded")) {
    throw "dashboard Agent Lab proxy failed: $($agentLab | ConvertTo-Json -Compress)"
  }

  $platform = Invoke-RestMethod "http://127.0.0.1:3000/api/control/platform-health" -TimeoutSec 15
  $componentNames = @($platform.components | ForEach-Object { $_.name })
  foreach($required in @("gateway","supervisor","telemetry","docker-control","mcp","agent-lab","agent-evaluator","host-agent","tailscale")) {
    if($componentNames -notcontains $required) {
      throw "platform health is missing component: $required"
    }
  }

  $maintenance = Invoke-RestMethod "http://127.0.0.1:3000/api/control/maintenance" -TimeoutSec 15
  if($null -eq $maintenance.operations -or $null -eq $maintenance.snapshots -or $null -eq $maintenance.opencode) {
    throw "dashboard maintenance control response is incomplete"
  }

  $network = Invoke-RestMethod "http://127.0.0.1:3000/api/control/network" -TimeoutSec 15
  if($null -eq $network.studio_routes) {
    throw "host-agent network status does not expose studio route state"
  }
}

Assert-GatewayProxy
$holder = "ai-stack-proxy-ip-holder"
$before = docker inspect ai-stack-gateway --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
try {
  docker rm -f $holder 2>$null | Out-Null
  docker compose stop gateway | Out-Host
  docker compose rm -f gateway | Out-Host
  docker run -d --name $holder --network ai-stack-net --ip $before alpine:3.20 sleep 120 | Out-Null
  if($LASTEXITCODE -ne 0) { throw "failed to occupy old gateway IP $before" }

  docker compose up -d gateway | Out-Host
  if($LASTEXITCODE -ne 0) { throw "gateway recreate failed" }
  for($i=0; $i -lt 60; $i++) {
    try {
      $direct = Invoke-RestMethod "http://127.0.0.1:8090/health" -TimeoutSec 2
      if($direct.service -eq "gateway" -and $direct.status -eq "ok") { break }
    } catch {}
    Start-Sleep -Milliseconds 500
  }
  if($i -ge 60) { throw "gateway did not become healthy after recreate" }

  $after = docker inspect ai-stack-gateway --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
  if($before -eq $after) { throw "gateway IP did not change despite occupied old address" }

  $proxyStarted = [DateTime]::UtcNow
  $proxyReady = $false
  for($j=0; $j -lt 20; $j++) {
    try {
      Assert-GatewayProxy
      $proxyReady = $true
      break
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  if(-not $proxyReady) { throw "dashboard proxy did not recover after forced gateway IP change" }
  $proxyRecovery = [math]::Round(([DateTime]::UtcNow - $proxyStarted).TotalSeconds, 2)
  Write-Host "Dashboard proxy survived a forced gateway IP change."
  Write-Host "Gateway IP before: $before"
  Write-Host "Gateway IP after:  $after"
  Write-Host "Proxy recovery:    $proxyRecovery s"
}
finally {
  docker rm -f $holder 2>$null | Out-Null
}
