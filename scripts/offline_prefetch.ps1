<#
.SYNOPSIS
  Pre-download and build all Docker/dashboard/model assets required for offline JARVIS startup.

.DESCRIPTION
  Run this once while internet is available. After it finishes, normal starts should use local
  Docker images, local BuildKit/package caches, local dashboard node_modules and local model files.

  This script intentionally does NOT prune caches. It prepares them.
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
function Ensure-DockerImage([string]$image) {
  if (-not $image) { return }
  Info "image: $image"
  docker image inspect $image *> $null
  if ($LASTEXITCODE -ne 0) { docker pull $image }
}

Info "Проверяю Docker"
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop не отвечает." }

if (Test-Path $ProfilesFile) {
  Info "Применяю профиль $Profile"
  python (Join-Path $Root "jarvis.py") up --profile $Profile --no-audio --offline-prepare-only
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
  Info "Проверяю/докачиваю модели профиля и синхронизирую их в jarvis-models"
  python (Join-Path $Root "jarvis.py") up --profile $Profile --no-audio --offline-prepare-only --download-models
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

Info "Готово. Для оффлайн-старта используйте: python jarvis.py up --offline"
