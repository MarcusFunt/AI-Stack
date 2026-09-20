#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Run-Step($Name, [scriptblock]$Action) {
  Write-Output ""
  Write-Output ("=== " + $Name + " ===")
  & $Action
  if ($LASTEXITCODE -ne 0) {
    throw "$Name failed with exit code $LASTEXITCODE"
  }
}

$labVolume = (Join-Path $Root "agent_lab") + ":/app/agent_lab:ro"
$evalVolume = (Join-Path $Root "agent_eval") + ":/app/agent_eval:ro"

Run-Step "Compose validation" {
  docker compose config | Out-Null
}

Run-Step "Python syntax" {
  python -m compileall -q agent_lab agent_eval scripts
}

Run-Step "Agent Lab unit tests" {
  docker compose run --rm --no-deps -v $labVolume agent-lab python -m unittest discover -s agent_lab/tests -v
}

Run-Step "Sandbox security test" {
  docker compose run --rm --no-deps -e AGENT_LAB_SANDBOX_TEST=1 -v $labVolume agent-lab-sandbox python -m unittest agent_lab.tests.test_sandbox -v
}

Run-Step "Agent Evaluator unit tests" {
  docker compose run --rm --no-deps -v $evalVolume agent-evaluator python -m unittest discover -s agent_eval/tests -v
}

Write-Output ""
Write-Output "ALL_CODE_TESTS_PASSED"
