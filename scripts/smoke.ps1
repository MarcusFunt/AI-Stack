#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

& python (Join-Path $PSScriptRoot "smoke.py")
if ($LASTEXITCODE -ne 0) {
  throw "end-to-end smoke suite failed"
}
