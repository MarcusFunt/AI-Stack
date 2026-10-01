#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$Container = "ai-stack-turn-proxy"

$running = & docker inspect --format '{{.State.Running}}' $Container
if ($LASTEXITCODE -ne 0 -or [string]$running -ne "true") {
  throw "TURN proxy container is not running; start it with scripts\ai.ps1 start coturn"
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

$stunProbe = @'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
from host_agent import voice_turn_listener_ready
ready = voice_turn_listener_ready()
print("Loopback STUN: " + ("passed" if ready else "failed"))
raise SystemExit(0 if ready else 1)
'@
& python -c $stunProbe
if ($LASTEXITCODE -ne 0) { throw "loopback STUN check failed" }

$capabilityProbe = @'
from pathlib import Path
values = {}
for line in Path("/proc/1/status").read_text(encoding="ascii").splitlines():
    key, separator, value = line.partition(":")
    if separator and key in {"CapEff", "CapBnd"}:
        values[key] = int(value.strip(), 16)
if set(values) != {"CapEff", "CapBnd"}:
    raise SystemExit("PID 1 capability masks are unavailable")
print("PID 1 CapEff={:x} CapBnd={:x}".format(values["CapEff"], values["CapBnd"]))
raise SystemExit(0 if values["CapEff"] == 0 and values["CapBnd"] == 0 else 1)
'@
$capabilityOutput = & docker exec $Container python -c $capabilityProbe
if ($LASTEXITCODE -ne 0) { throw "proxy PID 1 retains capabilities: $capabilityOutput" }
Write-Output $capabilityOutput

$egressProbe = @'
import socket
connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
connection.settimeout(3)
try:
    connection.connect(("1.1.1.1", 443))
except OSError:
    print("External TCP egress: blocked")
else:
    print("External TCP egress: unexpectedly allowed")
    raise SystemExit(1)
finally:
    connection.close()
'@
$egressOutput = & docker exec $Container python -c $egressProbe
if ($LASTEXITCODE -ne 0 -or $egressOutput -notcontains "External TCP egress: blocked") {
  throw "proxy external TCP egress check failed: $egressOutput"
}
Write-Output $egressOutput
Write-Output "TURN proxy acceptance passed; no Tailnet routes were changed."
