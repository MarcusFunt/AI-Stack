#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$embeddingDirectory = Join-Path $root "models\embedding"
$target = [IO.Path]::GetFullPath((Join-Path $root "models\embedding\embeddinggemma-2"))
$rootPrefix = $root.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if(-not $target.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Embedding model target must stay inside the repository models\embedding directory."
}

New-Item -ItemType Directory -Path $embeddingDirectory -Force | Out-Null
$resolvedEmbeddingDirectory = (Resolve-Path -LiteralPath $embeddingDirectory).Path
$resolvedEmbeddingPrefix = $resolvedEmbeddingDirectory.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if(-not $resolvedEmbeddingDirectory.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "The repository models\embedding directory resolves outside the repository."
}

$repository = "google/embeddinggemma-2"
$revision = "914f7f89142e33e77833254d9c9b90c3cef7303b"
$hf = Get-Command hf -ErrorAction SilentlyContinue
if(-not $hf) {
  throw "The modern Hugging Face CLI 'hf' is required. Install huggingface_hub, then rerun this script."
}

New-Item -ItemType Directory -Path $target -Force | Out-Null
$resolvedTarget = (Resolve-Path -LiteralPath $target).Path
if(-not $resolvedTarget.StartsWith($resolvedEmbeddingPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Embedding model target resolves outside the repository models\embedding directory."
}
& $hf.Source download $repository --revision $revision --local-dir $resolvedTarget
if($LASTEXITCODE -ne 0) {
  throw "hf download failed with exit code $LASTEXITCODE"
}

$config = Join-Path $target "config.json"
$processor = Join-Path $target "preprocessor_config.json"
if(-not (Test-Path -LiteralPath $config -PathType Leaf) -or -not (Test-Path -LiteralPath $processor -PathType Leaf)) {
  throw "The pinned model snapshot is incomplete: config.json or preprocessor_config.json is missing."
}

$provenance = [ordered]@{
  repository = $repository
  revision = $revision
  local_files_only = $true
}
$provenancePath = Join-Path $target "ai-stack-provenance.json"
[IO.File]::WriteAllText($provenancePath, ($provenance | ConvertTo-Json -Depth 4), [Text.Encoding]::UTF8)
Write-Output ("Installed " + $repository + " at " + $revision + " under models\embedding.")
