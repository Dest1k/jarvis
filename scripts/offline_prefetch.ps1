<#
.SYNOPSIS
  Pre-download and build all assets required for offline JARVIS startup.

.DESCRIPTION
  Run this once while internet is available. It prepares Docker images, local
  JARVIS build images, dashboard node_modules and model volume sync. It does not
  prune anything and does not start the runtime stack.
#>
param(
  [string]$Profile = "gemma4-mono",
  [switch]$SkipModels,
  [switch]$NoAudio
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Compose = Join-Path $Root "wsl/docker-compose.agents.yml"
$EnvFile = Join-Path $Root "wsl/.env"
$ProfilesFile = Join-Path $Root "wsl/profiles.json"
$Dashboard = Join-Path $Root "dashboard"

function Info($m) { Write-Host "[JARVIS offline] $m" -ForegroundColor Cyan }
function Read-EnvValue([string]$key, [string]$fallback = "") {
  if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
      if ($line -match "^$([regex]::Escape($key))=(.*)$") { return $Matches[1].Trim() }
    }
  }
  return $fallback
}
function Write-EnvUpdates([hashtable]$updates) {
  $lines = @()
  if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile) }
  $out = New-Object System.Collections.Generic.List[string]
  foreach ($line in $lines) {
    $skip = $false
    foreach ($k in $updates.Keys) { if ($line -match "^$([regex]::Escape($k))=") { $skip = $true; break } }
    if (-not $skip -and $line.Trim()) { [void]$out.Add($line) }
  }
  foreach ($k in $updates.Keys) { [void]$out.Add("$k=$($updates[$k])") }
  $dir = Split-Path -Parent $EnvFile
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
  [IO.File]::WriteAllText($EnvFile, (($out -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
}
function Ensure-DockerImage([string]$image) {
  if (-not $image) { return }
  Info "image: $image"
  docker image inspect $image *> $null
  if ($LASTEXITCODE -ne 0) { docker pull $image }
}

Info "Проверяю Docker"
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop не отвечает." }

$modelNames = New-Object System.Collections.Generic.List[string]
if (Test-Path $ProfilesFile) {
  $profiles = Get-Content $ProfilesFile -Raw | ConvertFrom-Json
  $profileObj = $profiles.$Profile
  if (-not $profileObj) { throw "Профиль '$Profile' не найден в $ProfilesFile" }
  Info "Применяю профиль $Profile в wsl/.env"
  $updates = @{}
  foreach ($part in @("dispatcher", "gui")) {
    $p = $profileObj.$part
    if ($p -and $p.env) {
      foreach ($prop in $p.env.PSObject.Properties) { $updates[$prop.Name] = [string]$prop.Value }
    }
    if ($p -and $p.name) { [void]$modelNames.Add([string]$p.name) }
    if ($p -and $p.repo -and $p.name -and -not $SkipModels -and -not ([string]$p.repo).StartsWith("local/")) {
      $dataDir = Read-EnvValue "JARVIS_DATA_DIR" "D:/jarvis/data"
      $dest = Join-Path (Join-Path $dataDir "models") ([string]$p.name)
      Info "HF модель: $($p.repo) -> $dest"
      python (Join-Path $Root "hf_downloader.py") ([string]$p.repo) --dest $dest
    }
  }
  Write-EnvUpdates $updates
} else {
  Info "profiles.json не найден, использую текущий wsl/.env"
}

$vllmImage = Read-EnvValue "JARVIS_VLLM_IMAGE" "vllm/vllm-openai:nightly"
Ensure-DockerImage $vllmImage
Ensure-DockerImage "python:3.11-slim"
Ensure-DockerImage "alpine:latest"

Info "Собираю backend/sandbox/audio в локальный кеш"
$env:DOCKER_BUILDKIT = "1"
$env:COMPOSE_DOCKER_CLI_BUILD = "1"
docker compose -f $Compose --env-file $EnvFile build backend sandbox
if (-not $NoAudio) { docker compose -f $Compose --env-file $EnvFile build audio-layer }

Info "Создаю named volumes, если их ещё нет"
docker volume create jarvis-models *> $null
docker volume create jarvis-hf *> $null
docker volume create jarvis-vllm-cache *> $null

if (-not $SkipModels) {
  $dataDir = Read-EnvValue "JARVIS_DATA_DIR" "D:/jarvis/data"
  $srcModels = Join-Path $dataDir "models"
  if (Test-Path $srcModels) {
    if ($modelNames.Count -eq 0) {
      $path = Read-EnvValue "JARVIS_QWEN_MODEL_PATH" ""
      if ($path) { [void]$modelNames.Add(($path.TrimEnd("/") -split "/")[-1]) }
    }
    $safeNames = @($modelNames | Where-Object { $_ -match '^[A-Za-z0-9_.-]+$' } | Select-Object -Unique)
    if ($safeNames.Count -gt 0) {
      Info "Синхронизирую модели в jarvis-models: $($safeNames -join ', ')"
      $list = $safeNames -join " "
      docker run --rm -v "jarvis-models:/dest" -v "${srcModels}:/src:ro" alpine sh -c "for n in $list; do if [ -d /src/`$n ]; then echo `$n; cp -ru /src/`$n /dest/; else echo missing:`$n; fi; done"
    }
  } else {
    Info "Папка моделей не найдена: $srcModels"
  }
}

if (Test-Path (Join-Path $Dashboard "package.json")) {
  if (-not (Test-Path (Join-Path $Dashboard "node_modules"))) {
    Info "Устанавливаю dashboard node_modules"
    Push-Location $Dashboard
    try { npm install --legacy-peer-deps } finally { Pop-Location }
  } else {
    Info "dashboard node_modules уже есть"
  }
}

Info "Финальная проверка локальных образов"
docker image inspect $vllmImage python:3.11-slim alpine:latest jarvis/backend:latest jarvis/sandbox:latest *> $null
if (-not $NoAudio) { docker image inspect jarvis/audio-layer:latest *> $null }

Info "Готово. Обычный старт после этого должен идти из локальных образов и кешей."
