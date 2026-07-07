param(
  [string]$Profile = "gemma4-mono"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "JARVIS offline start: profile=$Profile"
$env:JARVIS_PULL_POLICY = "never"
$env:COMPOSE_DOCKER_CLI_BUILD = "1"
$env:DOCKER_BUILDKIT = "1"

python jarvis.py up --profile $Profile
