#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-command local smoke check for JARVIS OS.

Run from repository root:
    python scripts/smoke_check.py

The script is intentionally conservative: it validates code, profiles, dashboard
build and compose config, but does not start vLLM or mutate runtime state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, cmd: list[str], *, cwd: Path = ROOT, optional: bool = False) -> bool:
    print(f"\n[SMOKE] {label}\n  $ {' '.join(cmd)}")
    try:
        p = subprocess.run(cmd, cwd=str(cwd), text=True)
    except FileNotFoundError as exc:
        print(f"[SMOKE] {'WARN' if optional else 'FAIL'}: {exc}")
        return optional
    ok = p.returncode == 0
    print(f"[SMOKE] {'OK' if ok else 'FAIL'} · {label}")
    return ok or optional


def check_profiles() -> bool:
    path = ROOT / "wsl" / "profiles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[SMOKE] FAIL · profiles.json unreadable: {exc}")
        return False
    ids = sorted(k for k in data if not k.startswith("_"))
    expected = ["gemma4-mono", "gemma4-turbo"]
    ok = ids == expected
    print(f"[SMOKE] {'OK' if ok else 'FAIL'} · profiles={ids}")
    if not ok:
        print(f"[SMOKE] expected exactly {expected}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-dashboard", action="store_true")
    ap.add_argument("--skip-compose", action="store_true")
    args = ap.parse_args()

    checks = [
        check_profiles(),
        run("backend compile", [sys.executable, "-m", "compileall", "backend"]),
        run("native/autonomy tests", [sys.executable, "backend/tests/test_native_runtime.py"]),
        run("agent tests", [sys.executable, "backend/tests/test_agent_system.py"]),
    ]
    if not args.skip_dashboard:
        checks.append(run("dashboard install", ["npm", "install", "--legacy-peer-deps"], cwd=ROOT / "dashboard"))
        checks.append(run("dashboard build", ["npm", "run", "build"], cwd=ROOT / "dashboard"))
    if not args.skip_compose:
        env = ROOT / "wsl" / ".env"
        env_file = env if env.exists() else ROOT / ".env.example"
        checks.append(run("compose config", ["docker", "compose", "-f", "wsl/docker-compose.agents.yml", "--env-file", str(env_file), "config"]))
    ok = all(checks)
    print("\n[SMOKE] RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
