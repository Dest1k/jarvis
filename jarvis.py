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
import json
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
PROFILES_FILE = ROOT / "wsl" / "profiles.json"
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


def _env_value(key: str) -> str:
    """Прочитать значение переменной из wsl/.env."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _data_dir_from_env() -> str:
    """JARVIS_DATA_DIR из wsl/.env (для синхронизации весов в том)."""
    return _env_value("JARVIS_DATA_DIR")


def _uitars_enabled() -> bool:
    """Включён ли отдельный UI-TARS. В СОЛО-режиме Gemma-4 он выключен
    (JARVIS_ENABLE_UITARS=0): зрение/GUI обслуживает сам мозг-диспетчер."""
    return _env_value("JARVIS_ENABLE_UITARS") != "0"


def _current_model_dirs() -> list[str]:
    """Имена папок моделей текущего .env (диспетчер [+ GUI]) — что синхронизировать.

    В СОЛО-режиме отдельной GUI-модели нет, поэтому UI-TARS из синка исключается.
    """
    names: set[str] = set()
    keys = [("JARVIS_QWEN_MODEL_PATH", "qwen-coder-14b")]
    if _uitars_enabled():
        keys.append(("JARVIS_UITARS_MODEL_PATH", "ui-tars"))
    for key, default in keys:
        val = _env_value(key) or f"/models/{default}"
        name = val.rstrip("/").split("/")[-1]
        if name:
            names.add(name)
    return sorted(names)


def sync_models(names: list[str] | None = None) -> None:
    """
    Скопировать веса из host-папки (9P) в ext4-том jarvis-models.

    Каталоги берутся ДИНАМИЧЕСКИ из активного профиля/.env (а не зашиты), чтобы
    работали любые модели (Gemma, UI-TARS-1.5 и т.д.), а не только дефолтные.
    """
    data = _data_dir_from_env()
    if not data:
        return
    names = names or _current_model_dirs()
    if not names:
        return
    src = f"{data}/models"
    info(f"Синхронизация весов в ext4-том: {', '.join(names)} …")
    namelist = " ".join(n for n in names if n.replace("-", "").replace("_", "").replace(".", "").isalnum())
    inner = (f'for n in {namelist}; do '
             'if [ -d "/src/$n" ]; then echo "  $n"; cp -ru "/src/$n" /dest/; '
             'else echo "  [нет в data/models] $n"; fi; done; echo SYNC_DONE')
    subprocess.run(["docker", "run", "--rm", "-v", "jarvis-models:/dest",
                    "-v", f"{src}:/src:ro", "alpine", "sh", "-c", inner])


def download_profile_models(profile_id: str) -> None:
    """
    Докачать модели профиля (диспетчер + GUI) через hf_downloader.py.

    Идемпотентно: hf_downloader пропускает уже скачанное (проверка sha256, докачка
    по Range). Для gated-моделей (Gemma) нужен токен в hf_token.txt.
    """
    prof = _load_profiles().get(profile_id)
    if not prof:
        return
    data = _data_dir_from_env()
    base = f"{data}/models" if data else "data/models"
    for part in ("dispatcher", "gui"):
        p = prof.get(part, {})
        repo, name = p.get("repo", ""), p.get("name", "")
        if not repo or not name:
            continue
        dest = f"{base}/{name}"
        info(f"Модель профиля: {repo} → {dest}")
        info("(уже скачанное не качается заново; gated-модели требуют hf_token.txt)")
        run([sys.executable, "hf_downloader.py", repo, "--dest", dest], cwd=ROOT)


def _load_profiles() -> dict:
    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
        data.pop("_comment", None)
        return data
    except Exception as exc:  # noqa: BLE001
        info(f"Не удалось прочитать профили ({exc}).")
        return {}


def cmd_profiles() -> int:
    """Показать доступные профили системы (диспетчер + GUI)."""
    profiles = _load_profiles()
    if not profiles:
        info("Профили не найдены (wsl/profiles.json).")
        return 1
    info("Доступные профили (python jarvis.py up --profile <id>):")
    for pid, p in profiles.items():
        info(f"  • {pid:16} — {p.get('label', '')}")
        if p.get("vram"):
            info(f"      VRAM: {p['vram']}")
    return 0


def apply_profile(profile_id: str) -> bool:
    """Записать env-переменные профиля в wsl/.env (диспетчер + GUI)."""
    profiles = _load_profiles()
    prof = profiles.get(profile_id)
    if not prof:
        info(f"Профиль '{profile_id}' не найден. Доступные: {', '.join(profiles) or '—'}")
        return False
    env: dict[str, str] = {}
    for part in ("dispatcher", "gui"):
        env.update(prof.get(part, {}).get("env", {}))
    # сливаем с существующим .env, заменяя совпадающие ключи
    existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = [l for l in existing.splitlines()
             if not any(l.startswith(k + "=") for k in env)]
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    info(f"Применён профиль '{profile_id}': {prof.get('label', '')}")
    info("Параметры vLLM записаны в wsl/.env; модели профиля скачаются "
         "автоматически на следующем шаге (gated-модели требуют hf_token.txt).")
    return True


def _compose(*args: str) -> int:
    return run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE), *args])


def free_vram() -> None:
    """Остановить GPU-контейнеры JARVIS — освободить видеопамять перед стартом."""
    info("Освобождаю VRAM: останавливаю vLLM и аудио…")
    _compose("stop", "vllm-qwen-coder", "vllm-ui-tars", "audio-layer")


def up_stack_sequential() -> None:
    """
    Поднять стек ПОСЛЕДОВАТЕЛЬНО.

    СОЛО-режим (gemma4-solo): один vLLM-инстанс — единый мозг Gemma-4, который сам
    видит экран; UI-TARS выключен (JARVIS_ENABLE_UITARS=0) и не поднимается.

    Раздельные профили (с UI-TARS): второй vLLM-инстанс должен профилировать память
    ПОСЛЕ полной загрузки первого, иначе оба не помещаются (см. кумулятивный util
    UI-TARS в profiles.json/.env). --wait ждёт healthcheck каждого vLLM.
    """
    info("vLLM #1 (мозг-диспетчер) — поднимаю и ЖДУ готовности (минуты при загрузке весов)…")
    _compose("up", "-d", "--wait", "--wait-timeout", "900", "--force-recreate",
             "--no-deps", "vllm-qwen-coder")
    if _uitars_enabled():
        info("vLLM #2 (UI-TARS, отдельное зрение) — поднимаю и ЖДУ готовности…")
        _compose("up", "-d", "--wait", "--wait-timeout", "600", "--force-recreate",
                 "--no-deps", "vllm-ui-tars")
    else:
        info("СОЛО-режим: отдельный UI-TARS не нужен — зрение/GUI ведёт сам мозг. "
             "Останавливаю UI-TARS, если был запущен…")
        _compose("stop", "vllm-ui-tars")
    info("Аудио, ядро, sandbox… (--build: подхватываю свежий код после git pull)")
    # --build обязателен: код ядра (orchestrator/) и mcp_servers.json ЗАШИТЫ в
    # образ jarvis/backend (не монтируются томом), поэтому после `git pull` без
    # пересборки контейнер крутил бы старый код. Слои с зависимостями кешируются
    # (BuildKit), так что при неизменных requirements пересборка быстрая —
    # переигрываются только COPY-слои. vLLM-сервисы собственного образа не имеют
    # (image:), их --build не трогает.
    # Сервисы перечислены явно (без --remove-orphans), чтобы не тронуть уже
    # поднятые vLLM-инстансы и сервис UI-TARS под compose-профилем.
    _compose("up", "-d", "--build", "audio-layer", "backend", "sandbox")


def cmd_freevram() -> int:
    """Принудительно освободить VRAM, занятую JARVIS (остановить GPU-контейнеры)."""
    free_vram()
    info("VRAM JARVIS освобождена. Память десктопа (браузер и пр.) освободите сами: "
         "закройте лишние вкладки/окна или отключите аппаратное ускорение в браузере.")
    return 0


def cmd_up(profile: str | None = None) -> int:
    if not ENV_FILE.exists():
        info("Не найден wsl/.env — похоже, система ещё не установлена.")
        info("Запустите установку: python jarvis.py install")
        return 1
    if profile:
        if not apply_profile(profile):
            return 1
    ensure_pydeps()
    if profile:
        # Автоскачивание моделей профиля (идемпотентно) ПЕРЕД подъёмом стека.
        info("Скачиваю/проверяю модели профиля (может занять время при первом разе)…")
        download_profile_models(profile)
    os.environ.setdefault("DOCKER_BUILDKIT", "1")

    cmd_bridge()

    sync_models()  # веса (по активному профилю) копируются в ext4-том

    free_vram()            # освободить VRAM от прежних инстансов
    up_stack_sequential()  # последовательный старт: vLLM#1 → vLLM#2 → аудио/ядро

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
    if not _uitars_enabled():
        info("Режим: СОЛО — единый мозг Gemma-4 (планирует, кодит и САМ видит "
             "экран). Отдельный UI-TARS выключен.")
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
                   choices=["up", "install", "stop", "status", "dashboard", "bridge",
                            "profiles", "freevram"])
    p.add_argument("--profile", default=None,
                   help="Профиль системы (мозг [+ зрение]) перед запуском, см. "
                        "`jarvis.py profiles`. Соло-режим (единый мозг Gemma-4): "
                        "--profile gemma4-solo")
    args, extra = p.parse_known_args()

    info("=" * 60)
    info(f"JARVIS-OS · единый лаунчер · команда: {args.command}")
    info("=" * 60)

    if args.command == "install":
        return cmd_install(extra)
    if args.command == "profiles":
        return cmd_profiles()
    if args.command == "freevram":
        return cmd_freevram()
    if args.command == "up":
        return cmd_up(profile=args.profile)
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
