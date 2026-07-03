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


def docker_ready() -> bool:
    """Быстрая проверка, что Docker-демон отвечает."""
    try:
        r = subprocess.run(["docker", "info"], capture_output=True,
                           text=True, timeout=25)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _start_docker_desktop() -> bool:
    """Запустить Docker Desktop (по известным путям или ярлыку)."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"),
        os.path.expandvars(r"%ProgramW6432%\Docker\Docker\Docker Desktop.exe"),
        os.path.expandvars(r"%LocalAppData%\Docker\Docker Desktop.exe"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            try:
                subprocess.Popen([exe], close_fds=True)
                return True
            except Exception:  # noqa: BLE001
                continue
    try:                       # последняя попытка — по ярлыку в PATH/Start
        subprocess.Popen('start "" "Docker Desktop"', shell=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _wait_docker(wait_secs: int) -> bool:
    """Подождать готовности Docker-демона (docker info) до wait_secs секунд."""
    for _ in range(max(1, wait_secs // 3)):
        time.sleep(3)
        if docker_ready():
            return True
    return False


def _kill_docker_desktop() -> None:
    """Мягко прибить процессы Docker Desktop перед перезапуском."""
    for name in ("Docker Desktop.exe", "com.docker.backend.exe",
                 "com.docker.build.exe", "com.docker.dev-envs.exe"):
        subprocess.run(["taskkill", "/F", "/IM", name],
                       capture_output=True, text=True)


def _disable_wsl_integration() -> bool:
    """
    Отключить WSL-интеграцию Docker Desktop с пользовательскими дистрибутивами.

    Лечит устойчивый краш старта Docker Desktop на этапе «setting up docker user
    group in <distro>» (Wsl/Service/0x800703e3): интеграция с Ubuntu-24.04
    НАШЕМУ стеку не нужна — контейнеры живут в служебном дистрибутиве
    docker-desktop, а docker CLI работает из Windows. Правит settings-store.json
    (или settings.json) с резервной копией .bak. Возвращает True, если что-то
    реально изменили.
    """
    appdata = os.environ.get("APPDATA", "")
    changed = False
    for fname in ("settings-store.json", "settings.json"):
        p = Path(appdata) / "Docker" / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        upd = {}
        # ключи различаются регистром между версиями Docker Desktop
        for key in ("integratedWslDistros", "IntegratedWslDistros"):
            if data.get(key):
                upd[key] = []
        for key in ("enableIntegrationWithDefaultWslDistro",
                    "EnableIntegrationWithDefaultWslDistro"):
            if data.get(key) is True:
                upd[key] = False
        if not upd:
            continue
        try:
            p.with_suffix(p.suffix + ".bak").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            data.update(upd)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            info(f"Отключил WSL-интеграцию Docker Desktop в {fname} "
                 f"(резервная копия: {fname}.bak).")
            changed = True
        except OSError as exc:
            info(f"Не удалось поправить {fname}: {exc}")
    return changed


def ensure_docker(wait_secs: int = 180) -> bool:
    """
    Гарантировать, что Docker-демон запущен, с многоступенчатым лечением.

    СТАДИЯ 1: запустить Docker Desktop и подождать.
    СТАДИЯ 2: не поднялся → типовое залипание WSL (Wsl/Service/0x800703e3,
        «Операция ввода/вывода была прервана…» на `wsl -d <distro> -e whoami`
        в фазе "setting up docker user group"). Лечение: убить Docker Desktop,
        `wsl --shutdown` (полный сброс WSL-ВМ), запустить заново.
    СТАДИЯ 3: упорствует → отключить WSL-интеграцию Docker Desktop с
        пользовательскими дистрибутивами (наш стек в ней не нуждается) и
        перезапустить ещё раз.
    """
    if docker_ready():
        return True
    info("Docker не отвечает — пробую запустить Docker Desktop…")
    if not _start_docker_desktop():
        info("Не нашёл Docker Desktop.exe. Запусти Docker Desktop вручную и повтори "
             "`python jarvis.py up`.")
        return False
    info(f"Жду готовности Docker (до {wait_secs} с, первый запуск дольше)…")
    if _wait_docker(wait_secs):
        info("Docker готов.")
        return True

    # --- СТАДИЯ 2: сброс WSL (классическое лечение Wsl/Service/0x800703e3) ---
    info("Docker не поднялся. Похоже на залипание WSL (Wsl/Service/0x800703e3). "
         "Сбрасываю WSL-ВМ и перезапускаю Docker Desktop…")
    _kill_docker_desktop()
    time.sleep(3)
    run(["wsl", "--shutdown"])
    time.sleep(8)          # дать службе WSL полностью остановить ВМ
    _start_docker_desktop()
    info(f"Повторное ожидание Docker (до {wait_secs} с)…")
    if _wait_docker(wait_secs):
        info("Docker готов (после сброса WSL).")
        return True

    # --- СТАДИЯ 3: отключить WSL-интеграцию (крашится именно она) ---
    info("Docker всё ещё молчит. Отключаю WSL-интеграцию Docker Desktop с "
         "пользовательскими дистрибутивами (стеку JARVIS она не нужна)…")
    if _disable_wsl_integration():
        _kill_docker_desktop()
        time.sleep(3)
        run(["wsl", "--shutdown"])
        time.sleep(8)
        _start_docker_desktop()
        info(f"Финальное ожидание Docker (до {wait_secs} с)…")
        if _wait_docker(wait_secs):
            info("Docker готов (после отключения WSL-интеграции).")
            return True

    info("Docker так и не ответил. Вручную: открой Docker Desktop → дождись "
         "«Engine running»; если крашится с Wsl/Service/0x800703e3 — Settings → "
         "Resources → WSL Integration → сними галочку с Ubuntu-24.04, затем "
         "`wsl --shutdown` и повтори `python jarvis.py up`.")
    return False


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


def up_stack_sequential(skip_uitars: bool = False) -> None:
    """
    Поднять стек. В ДВОЙНОМ режиме — последовательно (второй vLLM профилирует память
    после первого). Если UI-TARS отключён (СОЛО/монолит/moe-turbo — сам мозг видит
    экран, JARVIS_ENABLE_UITARS=0) — поднимаем ТОЛЬКО диспетчер, а UI-TARS НЕ
    запускаем и останавливаем: иначе он отъедает VRAM и падает в цикле (OOM).
    """
    info("vLLM #1 (мозг-диспетчер) — поднимаю и ЖДУ готовности (минуты при загрузке весов)…")
    _compose("up", "-d", "--wait", "--wait-timeout", "900", "--force-recreate",
             "--no-deps", "vllm-qwen-coder")
    if skip_uitars:
        info("UI-TARS отключён (СОЛО/moe-turbo) — останавливаю его (если был запущен), "
             "чтобы не занимал VRAM и не падал в цикле.")
        _compose("stop", "vllm-ui-tars")
        _compose("rm", "-f", "vllm-ui-tars")
    else:
        info("vLLM #2 (UI-TARS) — поднимаю и ЖДУ готовности…")
        _compose("up", "-d", "--wait", "--wait-timeout", "600", "--force-recreate",
                 "--no-deps", "vllm-ui-tars")
    info("Аудио, ядро, sandbox… (--build: подхватываю свежий код после git pull)")
    # --build обязателен: код ядра (orchestrator/) и mcp_servers.json ЗАШИТЫ в
    # образ jarvis/backend (не монтируются томом), поэтому после `git pull` без
    # пересборки контейнер крутил бы старый код. Слои с зависимостями кешируются
    # (BuildKit), так что при неизменных requirements пересборка быстрая —
    # переигрываются только COPY-слои. vLLM-сервисы собственного образа не имеют
    # (image:), их --build не трогает.
    if skip_uitars:
        # НЕ поднимаем vllm-ui-tars (иначе `up --remove-orphans` поднял бы заново).
        _compose("up", "-d", "--build", "audio-layer", "backend", "sandbox")
    else:
        _compose("up", "-d", "--build", "--remove-orphans")


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

    # Docker обязателен для всего стека — поднимаем/ждём ДО операций с контейнерами,
    # иначе раньше сыпались «cannot find …dockerDesktopLinuxEngine» и стек не вставал.
    if not ensure_docker():
        return 1

    sync_models()  # веса (по активному профилю) копируются в ext4-том

    # UI-TARS не поднимаем, если профиль монолитный (mono:true) ИЛИ активный .env
    # выключил его (JARVIS_ENABLE_UITARS=0 — режим moe-turbo/СОЛО). Раньше учитывался
    # только mono, поэтому после применения moe-turbo обычный `up` всё равно
    # поднимал UI-TARS → OOM и цикличное падение контейнера. Теперь — честно по .env.
    mono = bool(_load_profiles().get(profile, {}).get("mono")) if profile else False
    skip_uitars = mono or not _uitars_enabled()

    free_vram()                     # освободить VRAM от прежних инстансов
    up_stack_sequential(skip_uitars)  # СОЛО/moe: только Gemma; иначе vLLM#1 → vLLM#2 → ядро

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
    if not docker_ready():
        info("Docker не запущен — контейнерный стек и так остановлен.")
    else:
        info("Останавливаю контейнерный стек…")
        run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE), "down"])
    info("Окна RPC-моста и дашборда закройте вручную (или Ctrl+C в них).")
    return 0


def cmd_status() -> int:
    if docker_ready():
        run(["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE), "ps"])
    else:
        info("Docker не запущен (запусти Docker Desktop или `python jarvis.py up`).")
    info(f"RPC-мост (8765): {'РАБОТАЕТ' if port_open(8765) else 'нет'}")
    info(f"Дашборд (3000):  {'РАБОТАЕТ' if port_open(3000) else 'нет'}")
    return 0


def cmd_install(extra: list[str]) -> int:
    info("Запускаю полную установку (автономный агент)…")
    return run([sys.executable, "install_agent.py", *extra], cwd=ROOT)


HELP_TEXT = r"""
============================================================
  JARVIS-OS — локальный мультиагентный ассистент (Windows/Linux)
  Мозг-диспетчер (Gemma/Qwen) + GUI-актуатор UI-TARS («TARS») + MCP + память
============================================================

КОМАНДЫ:
  python jarvis.py up [--profile <id>]   Поднять ВСЁ: Docker → RPC-мост → стек
                                          (vLLM ×2 + аудио + ядро + sandbox) →
                                          дашборд → браузер. Без аргументов = up.
  python jarvis.py stop                   Остановить контейнерный стек.
  python jarvis.py status                 Статус контейнеров, моста, дашборда.
  python jarvis.py profiles               Список профилей (мозг + GUI).
  python jarvis.py dashboard              Только дашборд (Next.js, :3000).
  python jarvis.py bridge                 Только RPC-мост хоста (:8765).
  python jarvis.py freevram               Остановить vLLM/аудио — освободить VRAM.
  python jarvis.py install                Полная первичная установка.
  python jarvis.py help                   Этот экран.

ПРОФИЛИ (один пресет = связка моделей, см. `profiles`):
  ── v2.0 (Gemma 4, СОЛО, совпадают с кнопками «Инференс-режим» в Пульте) ──
  moe-turbo       ★ СОЛО: Gemma-4-26B-A4B (NVFP4) — быстрый, БЕЗ UI-TARS.
                  Для УЖЕ скачанной модели data/models/gemma4-26b-a4b-nvfp4.
                  util 0.85; зрение — сама Gemma (мультимодальная).
  dense-hybrid    ★ СОЛО: Gemma-4-31B-IT (NVFP4, 30.7B) + оффлоад в 128 ГБ RAM,
                  БЕЗ UI-TARS. Максимум качества (--quantization modelopt);
                  util 0.75, зрение — сама Gemma.
  ── прочие ──
  gemma4-mono     МОНОЛИТ на Gemma-4-26B-A4B (то же, что moe-turbo, util 0.82).
  gemma27-mono    ★★ МОНОЛИТ: Gemma-3-27B (AWQ) рулит ВСЕМ, UI-TARS не поднимается.
  gemma12-tars7   Gemma-4-12B (NVFP4) + UI-TARS-1.5-7B (AWQ) — двойная связка.
  gemma4-tars15   Gemma-4-26B-A4B MoE (NVFP4, util 0.62) + ОТДЕЛЬНЫЙ UI-TARS-2B.
  qwen-classic    Qwen2.5-Coder-14B (AWQ) + UI-TARS-2B.

  ВАЖНО: второй vLLM (UI-TARS) поднимают ТОЛЬКО двойные профили (*-tars*,
  qwen-classic). Все СОЛО-профили (moe-turbo, dense-hybrid, *-mono) ставят
  JARVIS_ENABLE_UITARS=0 — лаунчер честно читает флаг из wsl/.env и UI-TARS
  не запускает (на 32 ГБ рядом с жирной Gemma он падал в OOM-цикле).

ТИПИЧНЫЙ СЦЕНАРИЙ (ваша скачанная Gemma-4-26B-A4B):
  1) git pull origin main          # подтянуть свежий код (канон. ветка — main)
  2) python jarvis.py up --profile moe-turbo      # быстрый СОЛО, без UI-TARS
     # или качество:  python jarvis.py up --profile dense-hybrid
  3) Открыть http://localhost:3000 → вкладка «Чат».

ПОЛЕЗНОЕ:
  • Сменить мозг/профиль на лету — вкладка «Пульт» в дашборде.
  • «Мониторная» — живые логи всех контейнеров (туда смотреть при проблемах).
  • Чистильщик (вкладка «Пульт») находит дубли моделей в ext4-томе и кэш HF.
  • Порты: ядро :8000, vLLM :8001/:8002, аудио :8003, мост :8765, дашборд :3000.

ДИАГНОСТИКА:
  • «Docker не отвечает» — `up` сам поднимет Docker Desktop; либо запусти его и
    дождись «Engine running».
  • Docker Desktop крашится сразу после загрузки с Wsl/Service/0x800703e3
    («setting up docker user group in Ubuntu-24.04», «Операция ввода/вывода
    была прервана…») — `up` теперь лечит сам: сброс `wsl --shutdown` → рестарт
    Docker; при упорстве отключает WSL-интеграцию с пользовательскими
    дистрибутивами (стеку она не нужна; резервная копия настроек — .bak).
    Вручную: Docker Desktop → Settings → Resources → WSL Integration → снять
    галочку Ubuntu-24.04 → `wsl --shutdown` → повторить up.
  • «RPC-мост: нет» — закрой окно «JARVIS RPC» и запусти `python jarvis.py up`
    (или `python jarvis.py bridge`).
  • vLLM падает на старте — глянь «Мониторную»; частая причина — мало VRAM
    (возьми профиль gemma12-tars7) или неверный флаг.
  • Окружение/модели — файл wsl/.env (редактор есть в «Пульте»).
============================================================
"""


def cmd_help() -> int:
    print(HELP_TEXT)
    return 0


def main() -> int:
    _utf8_console()
    p = argparse.ArgumentParser(
        prog="jarvis.py",
        description="JARVIS-OS — единая точка запуска (Windows/Linux). "
                    "Без команды = `up`. Полная справка: `python jarvis.py help`.",
        epilog="Примеры:\n"
               "  python jarvis.py up --profile gemma12-tars7\n"
               "  python jarvis.py status\n"
               "  python jarvis.py help",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="up",
                   choices=["up", "install", "stop", "status", "dashboard", "bridge",
                            "profiles", "freevram", "help"],
                   help="up|stop|status|profiles|dashboard|bridge|freevram|install|help")
    p.add_argument("--profile", default=None,
                   help="Профиль системы (диспетчер+GUI) перед запуском, см. "
                        "`jarvis.py profiles`. Напр.: --profile gemma12-tars7")
    args, extra = p.parse_known_args()

    if args.command == "help":
        return cmd_help()

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
