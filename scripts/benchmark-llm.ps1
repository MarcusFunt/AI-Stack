#Requires -Version 5.1
[CmdletBinding()]
param(
  [ValidateSet("llm","reasoning")]
  [string]$Service = "llm",
  [string]$Prompt = "What is 17*19? Reply with just the number.",
  [int]$MaxTokens = 24
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

& (Join-Path $PSScriptRoot "ai.ps1") start $Service
$model = if ($Service -eq "reasoning") { "local-reasoning" } else { "local-fast" }
$target = "http://" + $Service + ":8080/v1/chat/completions"

$payload = @{
  model = $model
  messages = @(@{role="user"; content=$Prompt})
  max_tokens = $MaxTokens
  temperature = 0
} | ConvertTo-Json -Depth 8 -Compress
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))

$py = "import base64,json,os,time,urllib.request; d=base64.b64decode('$encoded'); h={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['LLAMA_API_KEY']}; q=urllib.request.Request('$target',data=d,headers=h,method='POST'); t=time.time(); r=urllib.request.urlopen(q,timeout=600); x=json.loads(r.read()); print(json.dumps({'elapsed_s':round(time.time()-t,3),'usage':x.get('usage'),'timings':x.get('timings')},indent=2))"
$raw = & docker exec ai-stack-supervisor python -c $py
if ($LASTEXITCODE -ne 0) { throw "benchmark request failed" }

$result = $raw | ConvertFrom-Json
$vram = (& nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits).Trim()
$layerKey = if ($Service -eq "reasoning") { "REASONING_GPU_LAYERS" } else { "LLM_GPU_LAYERS" }
$line = Get-Content .env | Where-Object { $_ -like "$layerKey=*" } | Select-Object -First 1
$layers = if ($line) { [int](($line -split "=",2)[1]) } else { $null }

$record = [ordered]@{
  timestamp = (Get-Date).ToString("o")
  service = $Service
  gpu_layers = $layers
  vram_mib = [int]$vram
  elapsed_s = $result.elapsed_s
  usage = $result.usage
  timings = $result.timings
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$path = Join-Path $Root ("data\benchmarks\" + $Service + "-" + $stamp + ".json")
$record | ConvertTo-Json -Depth 10 | Set-Content $path -Encoding UTF8
$record | ConvertTo-Json -Depth 10
Write-Output "Saved: $path"
