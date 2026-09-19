#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Pass($name,$detail) { Write-Output ("[PASS] " + $name + ": " + $detail) }
function Fail($name,$detail) { Write-Output ("[FAIL] " + $name + ": " + $detail); $script:Failures++ }
$script:Failures = 0

try { $dv = docker --version; Pass "docker" $dv } catch { Fail "docker" $_ }
try { $cv = docker compose version; Pass "compose" $cv } catch { Fail "compose" $_ }
try {
  $gpu = nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
  Pass "gpu" $gpu
} catch { Fail "gpu" $_ }

foreach($key in @("AI_API_KEY","SUPERVISOR_TOKEN","LLAMA_API_KEY","LLM_MODEL","REASONING_MODEL")) {
  if(Get-Content .env | Where-Object { $_ -like "$key=*" }) { Pass "env:$key" "present" }
  else { Fail "env:$key" "missing" }
}

foreach($file in @("models\llm\daily.gguf","models\llm\reasoning.gguf","config\models.json")) {
  if(Test-Path $file) {
    $item=Get-Item $file
    Pass "file:$file" (([math]::Round($item.Length/1GB,3)).ToString()+" GiB")
  } else { Fail "file:$file" "missing" }
}

try { docker compose --profile gpu config | Out-Null; Pass "compose-config" "valid" }
catch { Fail "compose-config" $_ }

$running = @(docker ps --format "{{.Names}}" | Where-Object { $_ -match "^ai-stack-(llm|reasoning|stt|tts|vlm|comfyui|wangp|lerobot)$" })
if($running.Count -le 1) { Pass "gpu-exclusivity" ($running -join ",") }
else { Fail "gpu-exclusivity" ($running -join ",") }

$drives=Get-PSDrive C,D -ErrorAction SilentlyContinue
foreach($d in $drives) { Pass ("disk:"+$d.Name) (([math]::Round($d.Free/1GB,1)).ToString()+" GiB free") }

if($Failures) { throw "$Failures doctor check(s) failed" }
Write-Output "ALL_DOCTOR_CHECKS_PASSED"
