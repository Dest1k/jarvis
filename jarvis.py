#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
jarvis.py — ЕДИНАЯ точка запуска JARVIS-OS. Одна команда поднимает всё.

Подкоманды:
    python jarvis.py install   — полная установка (автономный агент на LLM).
    python jarvis.py up         — запустить ВСЁ: RPC-мост + контейнерный стек +
                                   дашборд + открыть браузер (по умолчанию).
    python jarvis.py stop       — остановить контейнерный стек.
    python jarvis.py status     — статус сервисов.
    python jarvis.py dashboard  — только дашборд.
    python jarvis.py bridge     — только RPC-мост.

Без аргументов = `up`. На Windows удобнее двойной клик по jarvis.bat.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT / "wsl" / "docker-compose.agents.yml"
ENV_FILE = ROOT / "wsl" / ".env"
DASHBOARD = ROOT / "dashboard"


def _utf8_console() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def info(msg: str) -> None:
    print(f"[JARVIS] {msg}")


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def run(cmd, *, cwd: Path | None = None, check: bool = False) -> int:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, shell=isinstance(cmd, str)).returncode


def start_window(title: str, command: str, cwd: Path) -> None:
    """Запустить долгоживущий процесс в отдельном окне консоли (переживает лаунчер)."""
    subprocess.Popen(f'start "{title}" cmd /k {command}', shell=True, cwd=str(cwd))


def ensure_pydeps() -> None:
    for mod, pkg in (("requests", "requests"), ("websockets", "websockets")):
        try:
            __import__(mod)
        except ImportError:
            info(f"Устанавливаю {pkg}…")
            run([sys.executable, "-m", "pip", "install", "-q", pkg])


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
def cmd_bridge() -> int:
    if port_open(8765):
        info("RPC-мост уже запущен (порт 8765).")
        return 0
    info("Запускаю RPC-мост (окно 'JARVIS RPC')…")
    start_window("JARVIS RPC", "python windows_rpc_bridge.py", ROOT)
    return 0


def cmd_dashboard() -> int:
    if port_open(3000):
        info("Дашборд уже запущен (порт 3000).")
        return 0
    if not (DASHBOARD / "node_modules").exists():
        if not shutil.which("npm"):
            info("npm не найден — установите Node.js LTS (winget install OpenJS.NodeJS.LTS).")
            return 1
        info("Устанавливаю зависимости дашборда (npm install, один раз)…")
        # --legacy-peer-deps: страховка от конфликтов peer-зависимостей
        # (напр. пакеты, ещё не объявившие поддержку React 19 в peerDependencies).
        run("npm install --legacy-peer-deps", cwd=DASHBOARD)
    info("Запускаю дашборд (окно 'JARVIS Dashboard')…")
    start_window("JARVIS Dashboard", "npm run dev", DASHBOARD)
    return 0


def _data_dir_from_env() -> str:
    """Прочитать JARVIS_DATA_DIR из wsl/.env (для синхронизации весов в том)."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("JARVIS_DATA_DIR="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def sync_models() -> None:
    """Скопировать веса из host-папки (9P) в ext4-том jarvis-models (как в bootstrap)."""
    data = _data_dir_from_env()
    if not data:
        return
    src = f"{data}/models"
    info("Синхронизация весов в ext4-том (нужно для стабильной загрузки vLLM)…")
    # Те же каталоги, что в bootstrap (MODEL_LOCAL_DIRS): qwen-coder-14b, ui-tars.
    # При смене модели (маркер .jarvis_repo разный) — чистим устаревшие шарды в томе.
    inner = ('for n in qwen-coder-14b ui-tars; do '
             'if [ -d "/src/$n" ]; then '
             'sm=$(cat "/src/$n/.jarvis_repo" 2>/dev/null || echo ""); '
             'dm=$(cat "/dest/$n/.jarvis_repo" 2>/dev/null || echo ""); '
             'if [ "$sm" != "$dm" ] && [ -d "/dest/$n" ]; then '
             'echo "  $n изменилась — пересоздаю"; rm -rf "/dest/$n"; fi; '
             'echo "  $n"; cp -ru "/src/$n" /dest/; fi; done; '
             'echo SYNC_DONE')
    subprocess.run(["docker", "run", "--rm", "-v", "jarvis-models:/dest",
                    "-v", f"{src}:/src:ro", "alpine", "sh", "-c", inner])


def cmd_up() -> int:
    if not ENV_FILE.exists():
        info("Не найден wsl/.env — похоже, система ещё не установлена.")
        info("Запустите установку: python jarvis.py install")
        return 1
    ensure_pydeps()
    os.environ.setdefault("DOCKER_BUILDKIT", "1")

    cmd_bridge()

    sync_models()  # веса должны быть в ext4-томе, иначе vLLM упадёт на 9P

    info("Поднимаю контейнерный стек (docker compose up -d)…")
    run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE),
         "up", "-d", "--remove-orphans"])

    cmd_dashboard()

    info("Жду готовности дашборда…")
    for _ in range(40):
        if port_open(3000):
            break
        time.sleep(1)
    try:
        subprocess.Popen('start "" "http://localhost:3000"', shell=True)
    except Exception:  # noqa: BLE001
        pass

    info("=" * 60)
    info("Готово. Пульт управления: http://localhost:3000  (вкладка «Пульт»)")
    info("vLLM-модели прогреваются 1-2 мин — следите за статусом в дашборде.")
    info("Остановить всё: python jarvis.py stop")
    info("=" * 60)
    return 0


def cmd_stop() -> int:
    info("Останавливаю контейнерный стек…")
    run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE), "down"])
    info("Окна RPC-моста и дашборда закройте вручную (или Ctrl+C в них).")
    return 0


def cmd_status() -> int:
    run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE), "ps"])
    info(f"RPC-мост (8765): {'РАБОТАЕТ' if port_open(8765) else 'нет'}")
    info(f"Дашборд (3000):  {'РАБОТАЕТ' if port_open(3000) else 'нет'}")
    return 0


def cmd_install(extra: list[str]) -> int:
    info("Запускаю полную установку (автономный агент)…")
    return run([sys.executable, "install_agent.py", *extra], cwd=ROOT)


def main() -> int:
    _utf8_console()
    p = argparse.ArgumentParser(description="JARVIS-OS — единая точка запуска.")
    p.add_argument("command", nargs="?", default="up",
                   choices=["up", "install", "stop", "status", "dashboard", "bridge"])
    args, extra = p.parse_known_args()

    info("=" * 60)
    info(f"JARVIS-OS · единый лаунчер · команда: {args.command}")
    info("=" * 60)

    if args.command == "install":
        return cmd_install(extra)
    if args.command == "up":
        return cmd_up()
    if args.command == "stop":
        return cmd_stop()
    if args.command == "status":
        return cmd_status()
    if args.command == "dashboard":
        return cmd_dashboard()
    if args.command == "bridge":
        return cmd_bridge()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
