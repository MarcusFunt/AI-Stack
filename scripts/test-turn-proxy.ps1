#Requires -Version 5.1
[CmdletBinding()]
param(
  [switch]$ProbeSelfTest
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProbeScript = Join-Path $PSScriptRoot "turn_proxy_acceptance_probe.py"

if ($ProbeSelfTest) {
  & python $ProbeScript self-test
  if ($LASTEXITCODE -ne 0) { throw "PowerShell/Python probe self-test failed" }
  exit 0
}

Set-Location $Root
$Container = "ai-stack-turn-proxy"
$Coturn = "ai-stack-coturn"

$running = & docker inspect --format '{{.State.Running}}' $Container
if ($LASTEXITCODE -ne 0 -or [string]$running -ne "true") {
  throw "TURN proxy container is not running; start it with scripts\ai.ps1 start coturn"
}

$proxyHealth = & docker inspect --format '{{.State.Health.Status}}' $Container
if ($LASTEXITCODE -ne 0 -or [string]$proxyHealth -ne "healthy") {
  throw "TURN proxy has not passed its STUN healthcheck"
}
$coturnHealth = & docker inspect --format '{{.State.Health.Status}}' $Coturn
if ($LASTEXITCODE -ne 0 -or [string]$coturnHealth -ne "healthy") {
  throw "coturn has not passed its TCP STUN healthcheck"
}

$portsJson = & docker inspect --format '{{json .NetworkSettings.Ports}}' $Container
if ($LASTEXITCODE -ne 0) { throw "could not inspect the TURN proxy port mapping" }
try { $ports = $portsJson | ConvertFrom-Json } catch { throw "TURN proxy port mapping is not valid JSON" }
$portProperties = @($ports.PSObject.Properties)
if ($portProperties.Count -ne 1 -or $portProperties[0].Name -ne "3478/tcp") {
  throw "TURN proxy must publish only TCP 3478"
}
$bindings = @($portProperties[0].Value)
if ($bindings.Count -ne 1 -or $bindings[0].HostIp -ne "127.0.0.1" -or [string]$bindings[0].HostPort -ne "3478") {
  throw "TURN proxy TCP 3478 must bind only to 127.0.0.1:3478"
}
Write-Output "Host publication: 127.0.0.1:3478/tcp"

$proxyNetworksJson = & docker inspect --format '{{json .NetworkSettings.Networks}}' $Container
if ($LASTEXITCODE -ne 0) { throw "could not inspect TURN proxy network membership" }
$coturnNetworksJson = & docker inspect --format '{{json .NetworkSettings.Networks}}' $Coturn
if ($LASTEXITCODE -ne 0) { throw "could not inspect coturn network membership" }
try {
  $proxyNetworks = @((($proxyNetworksJson | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $_.Name }) | Sort-Object)
  $coturnNetworks = @((($coturnNetworksJson | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $_.Name }) | Sort-Object)
} catch { throw "TURN network membership is not valid JSON" }
if (($proxyNetworks -join ",") -ne "ai-stack-turn-publish-net,ai-stack-voice-net") {
  throw "TURN proxy must be attached only to the publish and voice networks"
}
if (($coturnNetworks -join ",") -ne "ai-stack-voice-net") {
  throw "coturn must be attached only to the private voice network"
}

$coturnPortsJson = & docker inspect --format '{{json .NetworkSettings.Ports}}' $Coturn
if ($LASTEXITCODE -ne 0) { throw "could not inspect coturn port publication" }
try { $coturnPorts = $coturnPortsJson | ConvertFrom-Json } catch { throw "coturn port mapping is not valid JSON" }
foreach ($property in @($coturnPorts.PSObject.Properties)) {
  if ($null -ne $property.Value -and @($property.Value).Count -gt 0) {
    throw "coturn must not publish any host ports"
  }
}

& python $ProbeScript host-stun
if ($LASTEXITCODE -ne 0) { throw "loopback STUN check failed" }

$probeSource = Get-Content -LiteralPath $ProbeScript -Raw -Encoding UTF8
$probeOutput = $probeSource | & docker exec -i $Container python - container
if ($LASTEXITCODE -ne 0 -or $probeOutput -notcontains "External TCP egress: blocked") {
  throw "proxy capability or external egress check failed: $probeOutput"
}
Write-Output $probeOutput

Write-Output "TURN proxy acceptance passed; no Tailnet routes were changed."
