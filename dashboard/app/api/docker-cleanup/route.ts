import { NextRequest, NextResponse } from "next/server";
import { exec as execCb } from "node:child_process";
import { promisify } from "node:util";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const exec = promisify(execCb);
const MAX = 40000;

type RunResult = { cmd: string; ok: boolean; code?: number | null; out: string };

function clip(text: string, max = MAX) {
  if (!text) return "";
  if (text.length <= max) return text;
  const head = Math.floor(max / 2);
  return text.slice(0, head) + `\n…[вырезано ${text.length - max} символов]…\n` + text.slice(-head);
}

async function run(cmd: string, timeout = 120000): Promise<RunResult> {
  try {
    const r = await exec(cmd, { timeout, windowsHide: true, maxBuffer: 1024 * 1024 * 12 });
    return { cmd, ok: true, code: 0, out: clip(`${r.stdout || ""}${r.stderr || ""}`) };
  } catch (e: any) {
    return { cmd, ok: false, code: e?.code ?? null, out: clip(`${e?.stdout || ""}${e?.stderr || ""}${e?.message ? `\n${e.message}` : ""}`) };
  }
}

async function runMany(cmds: string[], timeout = 120000) {
  const results: RunResult[] = [];
  for (const cmd of cmds) results.push(await run(cmd, timeout));
  return results;
}

function psCompactScript() {
  return `$ErrorActionPreference='Continue'
Write-Host 'JARVIS Docker VHDX compact: stopping Docker Desktop and WSL...'
docker system prune -af
try { docker builder prune -af } catch {}
Get-Process 'Docker Desktop','com.docker.backend','com.docker.build','com.docker.dev-envs' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
wsl --shutdown
Start-Sleep -Seconds 3
$paths=@(
  "$env:LOCALAPPDATA\\Docker\\wsl\\data\\ext4.vhdx",
  "$env:LOCALAPPDATA\\Docker\\wsl\\data\\docker_data.vhdx"
) | Where-Object { Test-Path $_ }
if (-not $paths) { Write-Host 'VHDX not found under Docker\\wsl\\data'; pause; exit }
foreach($p in $paths){
  Write-Host "Compacting $p"
  if (Get-Command Optimize-VHD -ErrorAction SilentlyContinue) {
    Optimize-VHD -Path $p -Mode Full
  } else {
    $tmp=New-TemporaryFile
    @("select vdisk file=\"$p\"","attach vdisk readonly","compact vdisk","detach vdisk") | Set-Content -Encoding ASCII $tmp
    diskpart /s $tmp
    Remove-Item $tmp -Force
  }
}
$dockerExe=@("$env:ProgramFiles\\Docker\\Docker\\Docker Desktop.exe", "$env:LocalAppData\\Docker\\Docker Desktop.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if($dockerExe){ Start-Process $dockerExe }
Write-Host 'Done. If Docker Desktop is still starting, wait a minute.'
pause`;
}

export async function GET() {
  const info = await runMany([
    "docker system df -v",
    "docker builder du",
    "docker ps -a --size --format \"table {{.Names}}\\t{{.Status}}\\t{{.Size}}\"",
    "docker images --format \"table {{.Repository}}\\t{{.Tag}}\\t{{.Size}}\\t{{.ID}}\"",
    "docker volume ls",
  ], 90000);
  return NextResponse.json({ ok: info.every((x) => x.ok), results: info, ts: Date.now() });
}

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const mode = String(body.mode || "safe");
  if (mode === "safe") {
    const results = await runMany([
      "docker container prune -f",
      "docker image prune -f",
      "docker network prune -f",
      "docker builder prune -f --filter until=72h",
      "docker system df -v",
    ], 180000);
    return NextResponse.json({ ok: results.every((x) => x.ok), mode, results });
  }
  if (mode === "deep") {
    if (body.confirm !== true) return NextResponse.json({ ok: false, error: "Нужно явное подтверждение." }, { status: 400 });
    const results = await runMany([
      "docker container prune -f",
      "docker image prune -af",
      "docker network prune -f",
      "docker builder prune -af",
      "docker system df -v",
    ], 300000);
    return NextResponse.json({ ok: results.every((x) => x.ok), mode, results });
  }
  if (mode === "volumes") {
    if (body.confirm !== true) return NextResponse.json({ ok: false, error: "Нужно явное подтверждение." }, { status: 400 });
    const ps = "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$vols=docker volume ls -q | ? {$_ -notlike 'jarvis*'}; foreach($v in $vols){ docker volume rm $v 2>$null }; docker volume ls\"";
    const results = await runMany([ps, "docker system df -v"], 240000);
    return NextResponse.json({ ok: results.every((x) => x.ok), mode, results });
  }
  if (mode === "compact") {
    if (body.confirmText !== "СЖАТЬ") return NextResponse.json({ ok: false, error: "Для сжатия VHDX введите СЖАТЬ." }, { status: 400 });
    const encoded = Buffer.from(psCompactScript(), "utf16le").toString("base64");
    const cmd = `start "JARVIS Docker Compact" powershell -NoExit -ExecutionPolicy Bypass -EncodedCommand ${encoded}`;
    const result = await run(cmd, 30000);
    return NextResponse.json({ ok: result.ok, mode, started: true, results: [result] });
  }
  return NextResponse.json({ ok: false, error: "Неизвестный режим очистки." }, { status: 400 });
}
