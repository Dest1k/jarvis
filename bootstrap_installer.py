#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bootstrap_installer.py — нативный Windows-загрузчик системы JARVIS-OS.

Назначение:
    1. Проверить готовность хост-окружения Windows 11 (версия ОС, WSL2, Docker,
       драйвер NVIDIA, nvidia-smi).
    2. Опросить локальную модель LM Studio (Gemma) по OpenAI-совместимому API,
       чтобы ДИНАМИЧЕСКИ сгенерировать параметры развёртывания (.wslconfig,
       флаги vLLM, профиль ресурсов).
    3. Проверить готовность NVIDIA Container Toolkit внутри WSL2.
    4. Внедрить оптимальные системные лимиты в %USERPROFILE%\\.wslconfig.
    5. Автоматически инициализировать контейнеризованный бэкенд внутри WSL2.

Запуск (PowerShell на хосте):
    python bootstrap_installer.py --lmstudio http://localhost:1234/v1 --wsl-ram 96 --wsl-cpus 20

ВАЖНО: скрипт выполняется НАТИВНО в Windows (не в WSL). Python 3.11+.
Единственная внешняя зависимость — `requests` (pip install requests).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    print("[ОШИБКА] Требуется пакет 'requests'. Установите: pip install requests")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Журналирование (все сообщения — на русском)
# --------------------------------------------------------------------------- #
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
log = logging.getLogger("jarvis.bootstrap")


# --------------------------------------------------------------------------- #
# Константы окружения
# --------------------------------------------------------------------------- #
WSL_DISTRO_DEFAULT = "Ubuntu-24.04"
REPO_DIR = Path(__file__).resolve().parent
WSLCONFIG_PATH = Path(os.path.expandvars(r"%USERPROFILE%")) / ".wslconfig"


# --------------------------------------------------------------------------- #
# Профиль развёртывания — структура, которую заполняет/уточняет LM Studio
# --------------------------------------------------------------------------- #
@dataclass
class DeploymentProfile:
    """Полный профиль развёртывания JARVIS-OS."""

    # Лимиты WSL2 (.wslconfig)
    wsl_memory_gb: int = 96
    wsl_processors: int = 20
    wsl_swap_gb: int = 16
    wsl_distro: str = WSL_DISTRO_DEFAULT
    nested_virtualization: bool = True

    # Профиль GPU/VRAM (RTX 5090, 32 ГБ)
    gpu_total_vram_gb: float = 32.0
    gpu_host_reserve_gb: float = 5.5  # жёсткий резерв под хост + Xvfb + KV-pool

    # Флаги vLLM по инстансам
    qwen_gpu_util: float = 0.60
    qwen_max_model_len: int = 32768
    uitars_gpu_util: float = 0.17

    # Произвольные заметки/обоснование от модели
    notes: list[str] = field(default_factory=list)

    def validate_vram(self) -> None:
        """Проверка непротиворечивости матрицы VRAM."""
        qwen = self.qwen_gpu_util * self.gpu_total_vram_gb
        uitars = self.uitars_gpu_util * self.gpu_total_vram_gb
        audio = 2.0
        used = qwen + uitars + audio
        headroom = self.gpu_total_vram_gb - used
        log.info(
            "Матрица VRAM: Qwen=%.1f ГБ, UI-TARS=%.1f ГБ, Audio=%.1f ГБ → "
            "занято %.1f ГБ, резерв %.1f ГБ",
            qwen, uitars, audio, used, headroom,
        )
        if headroom < self.gpu_host_reserve_gb - 0.3:
            raise ValueError(
                f"Недостаточный резерв VRAM: {headroom:.1f} ГБ < "
                f"требуемых {self.gpu_host_reserve_gb} ГБ. Снизьте gpu_util."
            )


# --------------------------------------------------------------------------- #
# Утилита запуска внешних процессов с понятным логированием
# --------------------------------------------------------------------------- #
def run(cmd: list[str], *, check: bool = True, capture: bool = True,
        timeout: int = 300) -> subprocess.CompletedProcess:
    """Запустить команду на хосте и вернуть результат."""
    log.debug("Выполняю: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=check,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result
    except FileNotFoundError:
        log.error("Команда не найдена: %s", cmd[0])
        raise
    except subprocess.TimeoutExpired:
        log.error("Превышено время ожидания команды: %s", " ".join(cmd))
        raise


# --------------------------------------------------------------------------- #
# ЭТАП 1. Проверки хост-окружения
# --------------------------------------------------------------------------- #
class HostChecker:
    """Набор проверок готовности хоста Windows."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def check_windows(self) -> None:
        log.info("→ Проверка версии Windows…")
        if platform.system() != "Windows":
            self.warnings.append(
                "Скрипт рассчитан на нативный запуск в Windows; "
                "обнаружена ОС: " + platform.system()
            )
            return
        ver = platform.version()
        log.info("  ОС: Windows %s (build %s)", platform.release(), ver)
        # Windows 11 → build >= 22000
        m = re.search(r"\.(\d{5,})$", ver)
        if m and int(m.group(1)) < 22000:
            self.warnings.append("Сборка Windows ниже 22000 — рекомендуется Windows 11.")

    def check_wsl(self) -> None:
        log.info("→ Проверка WSL2…")
        if not shutil.which("wsl"):
            self.errors.append("WSL не установлен. Выполните: wsl --install")
            return
        try:
            res = run(["wsl", "--status"], check=False)
            out = (res.stdout or "") + (res.stderr or "")
            if "2" not in out and "WSL 2" not in out and "Версия по умолчанию: 2" not in out:
                self.warnings.append(
                    "Не удалось подтвердить WSL2 по умолчанию. "
                    "Выполните: wsl --set-default-version 2"
                )
            log.info("  WSL обнаружен.")
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"Ошибка опроса WSL: {exc}")

    def check_docker(self) -> None:
        log.info("→ Проверка Docker Desktop…")
        if not shutil.which("docker"):
            self.errors.append("Docker не найден. Установите Docker Desktop с включённым WSL2-бэкендом.")
            return
        res = run(["docker", "version", "--format", "{{.Server.Version}}"], check=False)
        if res.returncode != 0:
            self.errors.append("Docker-демон недоступен. Запустите Docker Desktop.")
        else:
            log.info("  Docker server: %s", (res.stdout or "").strip())

    def check_nvidia(self) -> None:
        log.info("→ Проверка драйвера NVIDIA и nvidia-smi…")
        if not shutil.which("nvidia-smi"):
            self.errors.append("nvidia-smi не найден. Установите свежий драйвер NVIDIA (Blackwell / RTX 5090).")
            return
        res = run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            check=False,
        )
        if res.returncode != 0:
            self.errors.append("nvidia-smi завершился с ошибкой.")
            return
        info = (res.stdout or "").strip()
        log.info("  GPU: %s", info)
        if "5090" not in info:
            self.warnings.append(
                "Целевой GPU RTX 5090 не обнаружен явно — проверьте матрицу VRAM вручную."
            )

    def run_all(self) -> bool:
        self.check_windows()
        self.check_wsl()
        self.check_docker()
        self.check_nvidia()
        for w in self.warnings:
            log.warning("ВНИМАНИЕ: %s", w)
        for e in self.errors:
            log.error("КРИТИЧНО: %s", e)
        return not self.errors


# --------------------------------------------------------------------------- #
# ЭТАП 2. Опрос LM Studio (Gemma) для динамической генерации профиля
# --------------------------------------------------------------------------- #
class LMStudioClient:
    """Клиент OpenAI-совместимого API LM Studio."""

    def __init__(self, base_url: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _detect_model(self) -> str:
        """Определить идентификатор загруженной модели."""
        try:
            r = requests.get(f"{self.base_url}/models", timeout=10)
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                return data[0]["id"]
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить список моделей LM Studio: %s", exc)
        return "local-model"

    def generate_profile(self, host_facts: dict[str, Any],
                         base: DeploymentProfile) -> DeploymentProfile:
        """
        Передать модели факты о хосте и попросить уточнить профиль развёртывания.
        Модель возвращает строго JSON; при сбое используется безопасный дефолт.
        """
        model = self._detect_model()
        log.info("Опрашиваю LM Studio (модель: %s) для генерации конфигурации…", model)

        system_prompt = textwrap.dedent("""\
            Ты — инженер по развёртыванию локальной мультиагентной системы JARVIS-OS
            на Windows 11 (WSL2 + vLLM + Docker) с GPU NVIDIA RTX 5090 (32 ГБ VRAM).
            Тебе дают факты о хосте. Верни СТРОГО валидный JSON (без markdown, без пояснений)
            со следующими ключами:
              wsl_memory_gb (int), wsl_processors (int), wsl_swap_gb (int),
              qwen_gpu_util (float 0..1), qwen_max_model_len (int),
              uitars_gpu_util (float 0..1), notes (массив коротких строк на русском).
            Жёсткое ограничение: зарезервируй ~5.5 ГБ VRAM под хост, Xvfb и KV-кэш.
            Сумма (qwen_gpu_util + uitars_gpu_util) * 32 + 2 ГБ (аудио) не должна
            превышать 26.5 ГБ.
        """)

        user_prompt = "Факты о хосте (JSON):\n" + json.dumps(host_facts, ensure_ascii=False, indent=2)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 800,
            # LM Studio поддерживает принудительный JSON-режим у многих моделей
            "response_format": {"type": "json_object"},
        }

        try:
            r = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = self._extract_json(content)
            return self._merge_profile(base, data)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "LM Studio недоступен или вернул некорректный ответ (%s). "
                "Использую безопасный дефолтный профиль.", exc
            )
            base.notes.append("Профиль сгенерирован дефолтом: LM Studio не ответил.")
            return base

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        """Извлечь JSON-объект из ответа модели (с защитой от обёрток markdown)."""
        content = content.strip()
        # Срезаем возможные ```json … ``` ограждения
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
        if fence:
            content = fence.group(1)
        else:
            brace = re.search(r"\{.*\}", content, re.DOTALL)
            if brace:
                content = brace.group(0)
        return json.loads(content)

    @staticmethod
    def _merge_profile(base: DeploymentProfile, data: dict[str, Any]) -> DeploymentProfile:
        """Аккуратно влить значения модели в базовый профиль с валидацией диапазонов."""
        def clamp(val, lo, hi):
            return max(lo, min(hi, val))

        if "wsl_memory_gb" in data:
            base.wsl_memory_gb = int(clamp(data["wsl_memory_gb"], 8, 120))
        if "wsl_processors" in data:
            base.wsl_processors = int(clamp(data["wsl_processors"], 2, 24))
        if "wsl_swap_gb" in data:
            base.wsl_swap_gb = int(clamp(data["wsl_swap_gb"], 0, 64))
        if "qwen_gpu_util" in data:
            base.qwen_gpu_util = float(clamp(data["qwen_gpu_util"], 0.3, 0.7))
        if "qwen_max_model_len" in data:
            base.qwen_max_model_len = int(clamp(data["qwen_max_model_len"], 4096, 32768))
        if "uitars_gpu_util" in data:
            base.uitars_gpu_util = float(clamp(data["uitars_gpu_util"], 0.10, 0.30))
        if isinstance(data.get("notes"), list):
            base.notes.extend(str(n) for n in data["notes"][:10])
        return base


# --------------------------------------------------------------------------- #
# ЭТАП 3. Проверка NVIDIA Container Toolkit внутри WSL2
# --------------------------------------------------------------------------- #
def verify_container_toolkit(distro: str) -> bool:
    """
    Запустить тестовый контейнер с пробросом GPU внутри WSL2.
    Возвращает True, если nvidia-smi виден внутри контейнера.
    """
    log.info("→ Проверка NVIDIA Container Toolkit (тестовый контейнер с --gpus all)…")
    cmd = [
        "wsl", "-d", distro, "--",
        "docker", "run", "--rm", "--gpus", "all",
        "nvidia/cuda:12.6.0-base-ubuntu24.04", "nvidia-smi", "-L",
    ]
    res = run(cmd, check=False, timeout=180)
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 0 and "GPU" in out:
        log.info("  Toolkit готов. Контейнер видит GPU:\n%s", out.strip())
        return True
    log.error(
        "  NVIDIA Container Toolkit не готов. Установите внутри WSL2:\n"
        "    sudo apt-get install -y nvidia-container-toolkit\n"
        "    sudo nvidia-ctk runtime configure --runtime=docker\n"
        "  Вывод: %s", out.strip()
    )
    return False


# --------------------------------------------------------------------------- #
# ЭТАП 4. Генерация и запись .wslconfig
# --------------------------------------------------------------------------- #
def write_wslconfig(profile: DeploymentProfile) -> Path:
    """Сформировать и записать %USERPROFILE%\\.wslconfig с резервной копией."""
    log.info("→ Формирование .wslconfig…")
    content = textwrap.dedent(f"""\
        # ====================================================================
        #  .wslconfig — сгенерировано bootstrap_installer.py для JARVIS-OS
        #  Хост: Intel Core Ultra 9 285K / 128 ГБ DDR5 / RTX 5090
        # ====================================================================
        [wsl2]
        memory={profile.wsl_memory_gb}GB
        processors={profile.wsl_processors}
        swap={profile.wsl_swap_gb}GB
        nestedVirtualization={'true' if profile.nested_virtualization else 'false'}
        # Ускоренный сетевой режим (зеркалирование портов хоста ↔ WSL)
        networkingMode=mirrored
        # Освобождение неиспользуемой памяти обратно хосту
        autoMemoryReclaim=gradual
        # Поддержка GUI/Xvfb-приложений
        guiApplications=true

        [experimental]
        sparseVhd=true
        hostAddressLoopback=true
    """)

    if WSLCONFIG_PATH.exists():
        backup = WSLCONFIG_PATH.with_suffix(f".bak.{int(time.time())}")
        shutil.copy2(WSLCONFIG_PATH, backup)
        log.info("  Существующий .wslconfig сохранён в %s", backup)

    WSLCONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    WSLCONFIG_PATH.write_text(content, encoding="utf-8")
    log.info("  Записан %s", WSLCONFIG_PATH)
    log.warning(
        "  Для применения лимитов выполните: wsl --shutdown  (затем перезапустите WSL)."
    )
    return WSLCONFIG_PATH


# --------------------------------------------------------------------------- #
# ЭТАП 5. Инициализация контейнеризованного бэкенда в WSL2
# --------------------------------------------------------------------------- #
def copy_repo_into_wsl(distro: str, target: str = "~/jarvis") -> str:
    """
    Скопировать репозиторий в файловую систему WSL (быстрее, чем /mnt/c).
    Возвращает абсолютный путь внутри WSL.
    """
    log.info("→ Копирование ассетов JARVIS-OS внутрь WSL (%s)…", target)
    # Преобразуем windows-путь репозитория в путь /mnt/...
    win_path = str(REPO_DIR)
    res = run(["wsl", "-d", distro, "--", "wslpath", "-a", win_path], check=False)
    wsl_src = (res.stdout or "").strip() or f"/mnt/c"
    # Резолвим ~ внутри WSL
    home_res = run(["wsl", "-d", distro, "--", "bash", "-lc", "echo $HOME"], check=False)
    home = (home_res.stdout or "/root").strip()
    dest = target.replace("~", home)
    run(["wsl", "-d", distro, "--", "bash", "-lc",
         f"mkdir -p '{dest}' && cp -r '{wsl_src}/.' '{dest}/' && ls -la '{dest}'"],
        check=False)
    log.info("  Ассеты скопированы в %s (внутри WSL)", dest)
    return dest


def launch_backend(distro: str, wsl_repo: str, profile: DeploymentProfile) -> None:
    """Запустить оркестратор внутри WSL2: настройка + docker compose."""
    log.info("→ Запуск оркестратора развёртывания внутри WSL2…")
    env_exports = (
        f"export JARVIS_QWEN_GPU_UTIL={profile.qwen_gpu_util} "
        f"JARVIS_QWEN_MAX_LEN={profile.qwen_max_model_len} "
        f"JARVIS_UITARS_GPU_UTIL={profile.uitars_gpu_util}"
    )
    script = f"{wsl_repo}/wsl/wsl_setup_orchestrator.sh"
    cmd = [
        "wsl", "-d", distro, "--", "bash", "-lc",
        f"{env_exports} && chmod +x '{script}' && '{script}'",
    ]
    log.info("  Команда: %s", " ".join(cmd))
    # Стримим вывод в реальном времени
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write("    [WSL] " + line)
    proc.wait()
    if proc.returncode != 0:
        log.error("Оркестратор завершился с кодом %s", proc.returncode)
    else:
        log.info("  Бэкенд инициализирован успешно.")


# --------------------------------------------------------------------------- #
# Главная точка входа
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="JARVIS-OS — нативный Windows-загрузчик окружения.",
    )
    p.add_argument("--lmstudio", default="http://localhost:1234/v1",
                   help="Базовый URL OpenAI-совместимого API LM Studio.")
    p.add_argument("--distro", default=WSL_DISTRO_DEFAULT,
                   help="Имя дистрибутива WSL2.")
    p.add_argument("--wsl-ram", type=int, default=96,
                   help="Память для WSL2, ГБ (макс. ~96 при 128 ГБ хоста).")
    p.add_argument("--wsl-cpus", type=int, default=20,
                   help="Число процессоров для WSL2.")
    p.add_argument("--skip-checks", action="store_true",
                   help="Пропустить проверки хоста (отладка).")
    p.add_argument("--dry-run", action="store_true",
                   help="Только сгенерировать профиль и .wslconfig, без запуска бэкенда.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log.info("=" * 70)
    log.info("JARVIS-OS · bootstrap_installer · старт развёртывания")
    log.info("=" * 70)

    # --- ЭТАП 1: проверки хоста ---
    if not args.skip_checks:
        checker = HostChecker()
        if not checker.run_all():
            log.error("Проверки хоста не пройдены. Исправьте критические ошибки и повторите.")
            return 2
    else:
        log.warning("Проверки хоста пропущены по флагу --skip-checks.")

    # --- Сбор фактов о хосте для модели ---
    host_facts = {
        "os": platform.platform(),
        "cpu": platform.processor() or "Intel Core Ultra 9 285K",
        "logical_cpus": os.cpu_count(),
        "target_gpu": "NVIDIA RTX 5090 32GB",
        "total_ram_gb": 128,
        "requested_wsl_ram_gb": args.wsl_ram,
        "requested_wsl_cpus": args.wsl_cpus,
    }

    # --- ЭТАП 2: динамический профиль через LM Studio ---
    base_profile = DeploymentProfile(
        wsl_memory_gb=args.wsl_ram,
        wsl_processors=args.wsl_cpus,
        wsl_distro=args.distro,
    )
    profile = LMStudioClient(args.lmstudio).generate_profile(host_facts, base_profile)

    try:
        profile.validate_vram()
    except ValueError as exc:
        log.error("Профиль VRAM некорректен: %s. Откатываюсь к безопасным дефолтам.", exc)
        profile.qwen_gpu_util, profile.uitars_gpu_util = 0.60, 0.17
        profile.validate_vram()

    log.info("Итоговый профиль развёртывания:\n%s",
             json.dumps(asdict(profile), ensure_ascii=False, indent=2))

    # --- ЭТАП 4: .wslconfig ---
    write_wslconfig(profile)

    if args.dry_run:
        log.info("Режим --dry-run: пропускаю проверку toolkit и запуск бэкенда.")
        return 0

    # --- ЭТАП 3: NVIDIA Container Toolkit ---
    if not args.skip_checks and not verify_container_toolkit(args.distro):
        log.error("Прерывание: NVIDIA Container Toolkit не готов.")
        return 3

    # --- ЭТАП 5: копирование ассетов и запуск бэкенда ---
    wsl_repo = copy_repo_into_wsl(args.distro)
    # Сохраняем профиль рядом с ассетами для дальнейшего использования сервером
    profile_json = json.dumps(asdict(profile), ensure_ascii=False, indent=2)
    run(["wsl", "-d", args.distro, "--", "bash", "-lc",
         f"cat > '{wsl_repo}/deployment_profile.json' <<'JARVIS_EOF'\n{profile_json}\nJARVIS_EOF"],
        check=False)
    launch_backend(args.distro, wsl_repo, profile)

    log.info("=" * 70)
    log.info("Развёртывание завершено. Дашборд: http://localhost:3000")
    log.info("Не забудьте запустить RPC-мост на хосте: python windows_rpc_bridge.py")
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
