<#
.SYNOPSIS
  Start JARVIS from local caches without profile-triggered downloads.

.DESCRIPTION
  Applies a profile directly into wsl/.env, sets JARVIS_PULL_POLICY=never, and then
  runs `python jarvis.py up` WITHOUT passing --profile. This avoids the online
  model downloader path during offline starts.
#>
param(
  [string]$Profile = "gemma4-mono",
  [switch]$NoAudio
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root "wsl/.env"
$ProfilesFile = Join-Path $Root "wsl/profiles.json"

function Info($m) { Write-Host "[JARVIS offline] $m" -ForegroundColor Cyan }
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

if (-not (Test-Path $ProfilesFile)) { throw "profiles.json не найден: $ProfilesFile" }
$profiles = Get-Content $ProfilesFile -Raw | ConvertFrom-Json
$profileObj = $profiles.$Profile
if (-not $profileObj) { throw "Профиль '$Profile' не найден." }

$updates = @{ "JARVIS_PULL_POLICY" = "never" }
if ($NoAudio) { $updates["JARVIS_ENABLE_AUDIO"] = "0" }
foreach ($part in @("dispatcher", "gui")) {
  $p = $profileObj.$part
  if ($p -and $p.env) {
    foreach ($prop in $p.env.PSObject.Properties) { $updates[$prop.Name] = [string]$prop.Value }
  }
}
Info "Применяю профиль $Profile и включаю pull_policy=never"
Write-EnvUpdates $updates

Info "Стартую JARVIS без сетевого profile-download пути"
python (Join-Path $Root "jarvis.py") up
