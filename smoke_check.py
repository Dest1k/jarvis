#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_check.py — быстрый readiness gate для JARVIS OS.

Запуск:
    python smoke_check.py

Проверяет то, что чаще всего ломает cold-start: Python compile, автономные тесты,
profiles.json, compose config, Docker, backend/dashboard ports, MCP config и базовые
пути данных. Скрипт не запускает стек и не меняет системные сервисы; если у dashboard
нет node_modules, он установит npm-зависимости локально.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / "wsl" / ".env"
COMPOSE = ROOT / "wsl" / "docker-compose.agents.yml"
PROFILES = ROOT / "wsl" / "profiles.json"
MCP = ROOT / "backend" / "mcp_servers.json"
DASHBOARD = ROOT / "dashboard"


def run(cmd: list[str] | str, *, cwd: Path | None = None, timeout: int = 180) -> tuple[bool, str]:
    """Run a command and decode output robustly on Russian Windows consoles.

    Python 3.14 on Windows may default subprocess text decoding to cp1251, while
    child processes in this repo emit UTF-8. Capturing bytes and decoding with
    errors='replace' avoids reader-thread UnicodeDecodeError and mojibake.
    """
    try:
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8:replace")
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            shell=isinstance(cmd, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
            timeout=timeout,
        )
        out = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode == 0, out.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _tail(detail: str) -> str:
    return detail.strip().splitlines()[-1][:180] if detail and detail.strip() else ""


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✖"
    tail = _tail(detail)
    print(f"{mark} {name}{(' — ' + tail) if tail and not ok else ''}")
    return ok


def optional(name: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "○"
    tail = _tail(detail)
    print(f"{mark} {name}{(' — ' + tail) if tail and not ok else ''}")
    return ok


def main() -> int:
    failures = 0

    failures += not check("profiles.json exists", PROFILES.exists())
    if PROFILES.exists():
        try:
            profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
            active = [k for k in profiles if not k.startswith("_")]
            failures += not check("only Gemma 4 profiles are exposed", set(active) == {"gemma4-mono", "gemma4-turbo"}, ", ".join(active))
        except Exception as exc:  # noqa: BLE001
            failures += not check("profiles.json parse", False, str(exc))

    failures += not check(".env exists", ENV.exists(), "run python jarvis.py install or create wsl/.env")
    failures += not check("compose file exists", COMPOSE.exists())
    failures += not check("MCP config exists", MCP.exists())

    ok, out = run([sys.executable, "-m", "compileall", "backend"], cwd=ROOT, timeout=240)
    failures += not check("backend compileall", ok, out)

    ok, out = run([sys.executable, "backend/tests/test_native_runtime.py"], cwd=ROOT, timeout=180)
    failures += not check("native/autonomy tests", ok, out)

    ok, out = run([sys.executable, "backend/tests/test_agent_system.py"], cwd=ROOT, timeout=180)
    failures += not check("agent system tests", ok, out)

    ok, out = run(["docker", "info"], cwd=ROOT, timeout=25)
    optional("Docker daemon", ok, "not running; jarvis.py up will try to start Docker Desktop")

    if ENV.exists() and COMPOSE.exists():
        ok, out = run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV), "config"], cwd=ROOT, timeout=120)
        failures += not check("docker compose config", ok, out)

    if (DASHBOARD / "package.json").exists():
        if not (DASHBOARD / "node_modules").exists():
            ok, out = run("npm install --legacy-peer-deps", cwd=DASHBOARD, timeout=420)
            failures += not check("dashboard npm install", ok, out)
        ok, out = run("npm run build", cwd=DASHBOARD, timeout=300)
        failures += not check("dashboard build", ok, out)

    optional("RPC bridge port 8765", port_open(8765), "offline now; expected if stack is stopped")
    optional("backend port 8000", port_open(8000), "offline now; expected if stack is stopped")
    optional("dashboard port 3000", port_open(3000), "offline now; expected if stack is stopped")

    print("=" * 72)
    if failures:
        print(f"SMOKE FAILED: {failures} blocking check(s).")
        return 1
    print("SMOKE PASS: code/config checks look ready for cold-start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
