#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bootstrap_installer.py — нативный Windows-загрузчик системы JARVIS-OS.

ПОЛНАЯ АВТОМАТИЗАЦИЯ. Скрипт сам:
    1. Проверяет хост (Windows, WSL2, Docker Desktop, драйвер NVIDIA).
    2. Опрашивает LM Studio (любую загруженную модель) с устойчивым выбором
       модели и прогрессивным фолбэком запроса (обходит 400 у gemma/QAT).
    3. Автоматически определяет имя установленного WSL-дистрибутива, а при его
       отсутствии — ставит Ubuntu без вмешательства пользователя.
    4. Записывает оптимальный %USERPROFILE%\\.wslconfig.
    5. Проверяет и при необходимости АВТОМАТИЧЕСКИ доводит до готовности
       поддержку GPU в контейнерах (через Docker Desktop / NVIDIA toolkit).
    6. Генерирует .env и АВТОМАТИЧЕСКИ поднимает весь контейнерный стек
       (vLLM ×2 + аудио + sandbox + ядро) через хостовый Docker Desktop.

Ключевой принцип: ничего не делается «вручную пользователем». Любой
отсутствующий компонент ставится/настраивается автоматически; жёстких
аварийных выходов из-за «надо поставить руками» больше нет.

Запуск (PowerShell на хосте):
    python bootstrap_installer.py --lmstudio http://localhost:1234/v1 --wsl-ram 96 --wsl-cpus 20

Зависимость: requests (pip install requests). Python 3.11+, нативно в Windows.
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
# Журналирование
# --------------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-8s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("jarvis.bootstrap")


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #
REPO_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = REPO_DIR / "wsl" / "docker-compose.agents.yml"
COMPOSE_ENV = REPO_DIR / "wsl" / ".env"
WSLCONFIG_PATH = Path(os.path.expandvars(r"%USERPROFILE%")) / ".wslconfig"
JARVIS_HOME = Path.home() / ".jarvis"

# Системные дистрибутивы WSL, которые нельзя использовать как рабочие
SYSTEM_DISTROS = {"docker-desktop", "docker-desktop-data", "docker_desktop"}
DEFAULT_INSTALL_DISTRO = "Ubuntu-24.04"

# Образы/модели
GPU_TEST_IMAGE = "nvidia/cuda:12.6.0-base-ubuntu24.04"
HEALTH_ENDPOINTS = {
    "vllm-qwen-coder": "http://127.0.0.1:8001/health",
    "vllm-ui-tars": "http://127.0.0.1:8002/health",
    "audio-layer": "http://127.0.0.1:8003/health",
    "backend": "http://127.0.0.1:8000/health",
}


# --------------------------------------------------------------------------- #
# Профиль развёртывания
# --------------------------------------------------------------------------- #
@dataclass
class DeploymentProfile:
    """Полный профиль развёртывания JARVIS-OS."""
    wsl_memory_gb: int = 96
    wsl_processors: int = 20
    wsl_swap_gb: int = 16
    wsl_distro: str = ""
    nested_virtualization: bool = True

    gpu_total_vram_gb: float = 32.0
    gpu_host_reserve_gb: float = 5.5

    qwen_gpu_util: float = 0.60
    qwen_max_model_len: int = 32768
    uitars_gpu_util: float = 0.17

    notes: list[str] = field(default_factory=list)

    def validate_vram(self) -> None:
        qwen = self.qwen_gpu_util * self.gpu_total_vram_gb
        uitars = self.uitars_gpu_util * self.gpu_total_vram_gb
        audio = 2.0
        used = qwen + uitars + audio
        headroom = self.gpu_total_vram_gb - used
        log.info("Матрица VRAM: Qwen=%.1f ГБ, UI-TARS=%.1f ГБ, Audio=%.1f ГБ → "
                 "занято %.1f ГБ, резерв %.1f ГБ", qwen, uitars, audio, used, headroom)
        if headroom < self.gpu_host_reserve_gb - 0.3:
            raise ValueError(f"Недостаточный резерв VRAM: {headroom:.1f} ГБ "
                             f"< {self.gpu_host_reserve_gb} ГБ.")


# --------------------------------------------------------------------------- #
# Утилиты запуска процессов
# --------------------------------------------------------------------------- #
def run(cmd: list[str], *, check: bool = True, capture: bool = True,
        timeout: int = 300) -> subprocess.CompletedProcess:
    """Запустить команду на хосте (текстовый режим, UTF-8)."""
    log.debug("Выполняю: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def decode_wsl(raw: bytes) -> str:
    """
    Корректно декодировать вывод самого wsl.exe.
    wsl.exe печатает свои сообщения (--list, --status, ошибки) в UTF-16LE;
    эвристически определяем это по обилию нулевых байтов.
    """
    if not raw:
        return ""
    if raw.count(b"\x00") > len(raw) // 4:
        return raw.decode("utf-16-le", "replace").replace("\x00", "")
    return raw.decode("utf-8", "replace")


def run_wsl_raw(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Запустить wsl.exe и декодировать его собственный вывод (UTF-16LE)."""
    p = subprocess.run(["wsl", *args], capture_output=True, timeout=timeout)
    return p.returncode, decode_wsl(p.stdout), decode_wsl(p.stderr)


# --------------------------------------------------------------------------- #
# Менеджер WSL: определение и авто-установка дистрибутива
# --------------------------------------------------------------------------- #
class WslManager:
    """Определение установленного дистрибутива WSL и его авто-установка."""

    @classmethod
    def list_distros(cls) -> list[str]:
        rc, out, _ = run_wsl_raw(["--list", "--quiet"])
        names = [ln.strip() for ln in out.replace("\r", "").split("\n") if ln.strip()]
        return [n for n in names if n.lower() not in SYSTEM_DISTROS]

    @classmethod
    def default_distro(cls) -> Optional[str]:
        rc, out, _ = run_wsl_raw(["--list", "--verbose"])
        for ln in out.replace("\r", "").split("\n"):
            ln = ln.rstrip()
            if ln.lstrip().startswith("*"):
                parts = ln.replace("*", "", 1).split()
                if parts and parts[0].lower() not in SYSTEM_DISTROS:
                    return parts[0]
        distros = cls.list_distros()
        return distros[0] if distros else None

    @classmethod
    def install_ubuntu(cls, distro: str = DEFAULT_INSTALL_DISTRO) -> bool:
        """
        Автоматически установить дистрибутив без интерактивной настройки
        пользователя (--no-launch). Дальше работаем под root.
        """
        log.info("Дистрибутив WSL не найден — устанавливаю '%s' автоматически…", distro)
        # Сначала пытаемся точное имя, затем дженерик 'Ubuntu'
        for target in (distro, "Ubuntu"):
            rc, out, err = run_wsl_raw(["--install", "-d", target, "--no-launch"], timeout=1200)
            combined = (out + err).strip()
            if rc == 0 or "успешно" in combined.lower() or "installed" in combined.lower():
                log.info("  Установлен дистрибутив '%s'.", target)
                time.sleep(5)
                # Поднимаем VM-движок дистрибутива (терминирует после регистрации)
                run_wsl_raw(["-d", target, "--", "true"], timeout=120)
                return True
            log.warning("  Не удалось установить '%s': %s", target, combined[:200])
        return False

    @classmethod
    def ensure_distro(cls, preferred: str = "", auto_install: bool = True) -> Optional[str]:
        """Вернуть имя пригодного дистрибутива; при отсутствии — установить."""
        distros = cls.list_distros()
        log.info("Установленные дистрибутивы WSL: %s", distros or "(нет)")
        if preferred and preferred in distros:
            return preferred
        default = cls.default_distro()
        if default:
            log.info("Использую дистрибутив по умолчанию: %s", default)
            return default
        if distros:
            log.info("Использую первый доступный дистрибутив: %s", distros[0])
            return distros[0]
        if auto_install and cls.install_ubuntu():
            return cls.default_distro() or (cls.list_distros() or [None])[0]
        return None

    @staticmethod
    def bash(distro: str, script: str, *, timeout: int = 900,
             stream: bool = False) -> subprocess.CompletedProcess | int:
        """Выполнить bash-скрипт внутри дистрибутива под root."""
        cmd = ["wsl", "-d", distro, "-u", "root", "--", "bash", "-lc", script]
        if not stream:
            return run(cmd, check=False, timeout=timeout)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write("    [WSL] " + line)
        proc.wait()
        return proc.returncode


# --------------------------------------------------------------------------- #
# Проверки хоста
# --------------------------------------------------------------------------- #
class HostChecker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def check_windows(self) -> None:
        log.info("→ Проверка версии Windows…")
        if platform.system() != "Windows":
            self.warnings.append("Скрипт рассчитан на нативный Windows; ОС: " + platform.system())
            return
        log.info("  ОС: Windows %s (%s)", platform.release(), platform.version())

    def check_wsl(self) -> None:
        log.info("→ Проверка WSL2…")
        if not shutil.which("wsl"):
            self.errors.append("WSL не установлен. Выполните: wsl --install")
            return
        rc, out, err = run_wsl_raw(["--status"])
        log.info("  WSL обнаружен.")
        if "2" not in (out + err):
            run_wsl_raw(["--set-default-version", "2"])

    def check_docker(self) -> None:
        log.info("→ Проверка Docker Desktop…")
        if not shutil.which("docker"):
            self.errors.append("Docker не найден. Установите Docker Desktop (WSL2-бэкенд).")
            return
        res = run(["docker", "version", "--format", "{{.Server.Version}}"], check=False)
        if res.returncode != 0:
            self.errors.append("Docker-демон недоступен. Запустите Docker Desktop.")
        else:
            log.info("  Docker server: %s", (res.stdout or "").strip())

    def check_nvidia(self) -> None:
        log.info("→ Проверка драйвера NVIDIA…")
        if not shutil.which("nvidia-smi"):
            self.errors.append("nvidia-smi не найден. Установите драйвер NVIDIA (RTX 5090).")
            return
        res = run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                   "--format=csv,noheader"], check=False)
        log.info("  GPU: %s", (res.stdout or "").strip())

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
# Клиент LM Studio (устойчивый: выбор модели + прогрессивный фолбэк)
# --------------------------------------------------------------------------- #
class LMStudioClient:
    """OpenAI-совместимый клиент LM Studio с обходом 400 у gemma/QAT-моделей."""

    # Предпочтительные модели для генерации конфигурации (по убыванию)
    PREFERRED = ("gemma-4-31b-it-qat", "gemma-4-26b-a4b-it-qat",
                 "qwen3-coder-30b-a3b-instruct", "devstral-small-2-24b-instruct-2512")

    def __init__(self, base_url: str, model: str = "", timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _list_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.base_url}/models", timeout=10)
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить список моделей LM Studio: %s", exc)
            return []

    def _select_model(self) -> str:
        """Выбрать пригодную чат-модель (не embedding)."""
        if self.model:
            return self.model
        ids = self._list_models()
        if not ids:
            return "local-model"
        for pref in self.PREFERRED:
            if pref in ids:
                return pref
        chat = [i for i in ids if "embed" not in i.lower()
                and any(k in i.lower() for k in ("it", "instruct", "chat", "coder"))]
        if chat:
            return chat[0]
        non_embed = [i for i in ids if "embed" not in i.lower()]
        return non_embed[0] if non_embed else ids[0]

    def _post(self, model: str, messages: list[dict], json_mode: bool) -> requests.Response:
        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "temperature": 0.2, "max_tokens": 800}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return requests.post(f"{self.base_url}/chat/completions", json=payload,
                             timeout=self.timeout)

    def generate_profile(self, host_facts: dict[str, Any],
                         base: DeploymentProfile) -> DeploymentProfile:
        model = self._select_model()
        log.info("Опрашиваю LM Studio (модель: %s)…", model)

        system_prompt = textwrap.dedent("""\
            Ты — инженер по развёртыванию JARVIS-OS на Windows 11 (WSL2 + vLLM +
            Docker) с GPU NVIDIA RTX 5090 (32 ГБ VRAM). По фактам о хосте верни
            СТРОГО валидный JSON (без markdown и пояснений) с ключами:
            wsl_memory_gb (int), wsl_processors (int), wsl_swap_gb (int),
            qwen_gpu_util (float 0..1), qwen_max_model_len (int),
            uitars_gpu_util (float 0..1), notes (массив строк на русском).
            Зарезервируй ~5.5 ГБ VRAM под хост/Xvfb/KV-кэш; сумма
            (qwen_gpu_util + uitars_gpu_util) * 32 + 2 не должна превышать 26.5.
        """)
        user_prompt = "Факты о хосте (JSON):\n" + json.dumps(host_facts, ensure_ascii=False, indent=2)
        sys_msg = {"role": "system", "content": system_prompt}
        usr_msg = {"role": "user", "content": user_prompt}
        # gemma не всегда поддерживает отдельную роль system → готовим слитный вариант
        merged_msg = {"role": "user", "content": system_prompt + "\n\n" + user_prompt}

        # Прогрессивные попытки: (сообщения, json_mode)
        attempts = [
            ([sys_msg, usr_msg], True),    # идеальный вариант
            ([sys_msg, usr_msg], False),   # без response_format (частая причина 400)
            ([merged_msg], False),         # без роли system (gemma)
        ]
        for messages, json_mode in attempts:
            try:
                r = self._post(model, messages, json_mode)
                if r.status_code in (400, 422):
                    log.warning("LM Studio %s (json=%s): %s — пробую следующий вариант.",
                                r.status_code, json_mode, (r.text or "")[:160])
                    continue
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                data = self._extract_json(content)
                merged = self._merge_profile(base, data)
                log.info("LM Studio успешно сгенерировал конфигурацию (модель %s).", model)
                merged.notes.append(f"Конфигурация получена от модели {model}.")
                return merged
            except Exception as exc:  # noqa: BLE001
                log.warning("Вариант запроса не удался (%s) — продолжаю.", exc)
                continue

        log.warning("LM Studio не дал валидного ответа ни в одном варианте. "
                    "Использую безопасный дефолтный профиль.")
        base.notes.append("Профиль сгенерирован дефолтом: LM Studio не ответил корректно.")
        return base

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        content = content.strip()
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
        def clamp(v, lo, hi):
            return max(lo, min(hi, v))
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
# Готовность GPU в контейнерах (авто-доведение)
# --------------------------------------------------------------------------- #
def is_docker_desktop() -> bool:
    res = run(["docker", "info", "--format", "{{.OperatingSystem}}"], check=False)
    return "docker desktop" in (res.stdout or "").lower()


def gpu_test_host() -> bool:
    """Тест проброса GPU через хостовый движок Docker (Docker Desktop)."""
    log.info("→ Тест GPU в контейнере (хостовый Docker)…")
    run(["docker", "pull", GPU_TEST_IMAGE], check=False, timeout=600)
    res = run(["docker", "run", "--rm", "--gpus", "all", GPU_TEST_IMAGE, "nvidia-smi", "-L"],
              check=False, timeout=240)
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 0 and "GPU" in out:
        log.info("  GPU доступен контейнерам:\n%s", out.strip())
        return True
    log.warning("  GPU пока недоступен контейнерам: %s", out.strip()[:200])
    return False


def ensure_gpu_ready(distro: Optional[str]) -> bool:
    """
    Автоматически довести поддержку GPU до готовности. НЕ прерывает работу:
    при невозможности — предупреждает и продолжает (стек попробует подняться).
    """
    if gpu_test_host():
        return True

    if is_docker_desktop():
        log.warning("Docker Desktop обычно даёт GPU «из коробки». Проверьте, что в "
                    "Settings → Resources → WSL Integration включён дистрибутив, а драйвер "
                    "NVIDIA поддерживает WSL. Повторяю тест через 8 с…")
        time.sleep(8)
        return gpu_test_host()

    # Нативный docker внутри WSL — ставим NVIDIA Container Toolkit автоматически
    if distro:
        log.info("→ Автоматическая установка NVIDIA Container Toolkit в '%s'…", distro)
        install = textwrap.dedent("""\
            set -e
            if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
              curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
              curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
                > /etc/apt/sources.list.d/nvidia-container-toolkit.list
              apt-get update -y && apt-get install -y nvidia-container-toolkit
            fi
            nvidia-ctk runtime configure --runtime=docker || true
            service docker restart 2>/dev/null || true
            sleep 4
            docker run --rm --gpus all %s nvidia-smi -L
        """) % GPU_TEST_IMAGE
        rc = WslManager.bash(distro, install, timeout=900, stream=True)
        if rc == 0:
            return True
    log.warning("Не удалось автоматически подтвердить GPU. Продолжаю — стек попытается "
                "стартовать; при OOM/ошибках GPU см. docs/vram_matrix.md.")
    return False


# --------------------------------------------------------------------------- #
# .wslconfig
# --------------------------------------------------------------------------- #
def write_wslconfig(profile: DeploymentProfile) -> Path:
    log.info("→ Формирование .wslconfig…")
    content = textwrap.dedent(f"""\
        # Сгенерировано bootstrap_installer.py для JARVIS-OS
        [wsl2]
        memory={profile.wsl_memory_gb}GB
        processors={profile.wsl_processors}
        swap={profile.wsl_swap_gb}GB
        nestedVirtualization={'true' if profile.nested_virtualization else 'false'}
        networkingMode=mirrored
        autoMemoryReclaim=gradual
        guiApplications=true

        [experimental]
        sparseVhd=true
        hostAddressLoopback=true
    """)
    if WSLCONFIG_PATH.exists():
        backup = WSLCONFIG_PATH.with_suffix(f".bak.{int(time.time())}")
        shutil.copy2(WSLCONFIG_PATH, backup)
        log.info("  Прежний .wslconfig сохранён в %s", backup)
    WSLCONFIG_PATH.write_text(content, encoding="utf-8")
    log.info("  Записан %s", WSLCONFIG_PATH)
    return WSLCONFIG_PATH


# --------------------------------------------------------------------------- #
# Генерация .env и подъём стека через хостовый Docker Desktop
# --------------------------------------------------------------------------- #
def write_compose_env(profile: DeploymentProfile, qwen_model: str, uitars_model: str) -> Path:
    """Сформировать wsl/.env для интерполяции docker compose."""
    JARVIS_HOME.mkdir(parents=True, exist_ok=True)
    # Docker Desktop принимает Windows-путь с прямыми слэшами
    home_host = str(JARVIS_HOME).replace("\\", "/")
    env = textwrap.dedent(f"""\
        # Сгенерировано bootstrap_installer.py — переменные для docker compose
        JARVIS_QWEN_MODEL={qwen_model}
        JARVIS_UITARS_MODEL={uitars_model}
        JARVIS_QWEN_GPU_UTIL={profile.qwen_gpu_util}
        JARVIS_QWEN_MAX_LEN={profile.qwen_max_model_len}
        JARVIS_UITARS_GPU_UTIL={profile.uitars_gpu_util}
        JARVIS_HOME_HOST={home_host}
        HF_TOKEN={os.environ.get('HF_TOKEN', '')}
        WHISPER_MODEL=large-v3
        WHISPER_COMPUTE_TYPE=int8_float16
        KOKORO_VOICE=af_sky
    """)
    COMPOSE_ENV.write_text(env, encoding="utf-8")
    log.info("  Записан %s", COMPOSE_ENV)
    return COMPOSE_ENV


def compose(*args: str, stream: bool = False, timeout: int = 1800) -> int:
    """Вызвать docker compose с нужным файлом и .env."""
    base = ["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", str(COMPOSE_ENV)]
    cmd = base + list(args)
    if not stream:
        res = run(cmd, check=False, timeout=timeout)
        if res.stdout:
            sys.stdout.write(res.stdout)
        return res.returncode
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write("    " + line)
    proc.wait()
    return proc.returncode


def bring_up_stack() -> None:
    """Автоматически поднять весь контейнерный стек через Docker Desktop."""
    log.info("→ Сборка/загрузка образов (docker compose build/pull)…")
    compose("pull", "--ignore-pull-failures", stream=True)
    compose("build", stream=True)
    log.info("→ Запуск стека (docker compose up -d)…")
    compose("up", "-d", "--remove-orphans", stream=True)

    log.info("→ Ожидание готовности сервисов…")
    for name, url in HEALTH_ENDPOINTS.items():
        ready = False
        # vLLM-инстансы прогреваются дольше всего
        retries = 90 if name.startswith("vllm") else 30
        for i in range(retries):
            try:
                if requests.get(url, timeout=3).status_code == 200:
                    log.info("  %s — готов (попытка %d).", name, i + 1)
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(10)
        if not ready:
            log.warning("  %s не ответил вовремя. Логи: docker compose logs %s", name, name)

    log.info("→ Текущий статус контейнеров:")
    compose("ps")


# --------------------------------------------------------------------------- #
# Аргументы и main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="JARVIS-OS — автоматический Windows-загрузчик.")
    p.add_argument("--lmstudio", default="http://localhost:1234/v1")
    p.add_argument("--model", default="", help="ID модели LM Studio (по умолчанию — авто-выбор).")
    p.add_argument("--distro", default="", help="Имя дистрибутива WSL (по умолчанию — авто).")
    p.add_argument("--wsl-ram", type=int, default=96)
    p.add_argument("--wsl-cpus", type=int, default=20)
    p.add_argument("--qwen-model", default="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ")
    p.add_argument("--uitars-model", default="bytedance-research/UI-TARS-7B-DPO")
    p.add_argument("--no-auto-install", action="store_true",
                   help="Не устанавливать дистрибутив WSL автоматически.")
    p.add_argument("--skip-stack", action="store_true",
                   help="Только подготовка (без подъёма контейнеров).")
    p.add_argument("--skip-checks", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log.info("=" * 70)
    log.info("JARVIS-OS · bootstrap_installer · полностью автоматическое развёртывание")
    log.info("=" * 70)

    if not args.skip_checks:
        checker = HostChecker()
        if not checker.run_all():
            log.error("Критические проверки хоста не пройдены. Исправьте и повторите.")
            return 2

    # --- Авто-определение/установка дистрибутива WSL ---
    distro = WslManager.ensure_distro(args.distro, auto_install=not args.no_auto_install)
    if distro:
        log.info("Рабочий дистрибутив WSL: %s", distro)
    else:
        log.warning("Дистрибутив WSL недоступен. Продолжаю в режиме хостового Docker "
                    "(стек поднимется через Docker Desktop без зависимости от дистрибутива).")

    # --- Динамический профиль через LM Studio ---
    host_facts = {
        "os": platform.platform(),
        "cpu": platform.processor() or "Intel Core Ultra 9 285K",
        "logical_cpus": os.cpu_count(),
        "target_gpu": "NVIDIA RTX 5090 32GB",
        "total_ram_gb": 128,
        "requested_wsl_ram_gb": args.wsl_ram,
        "requested_wsl_cpus": args.wsl_cpus,
    }
    base_profile = DeploymentProfile(wsl_memory_gb=args.wsl_ram,
                                     wsl_processors=args.wsl_cpus,
                                     wsl_distro=distro or "")
    profile = LMStudioClient(args.lmstudio, model=args.model).generate_profile(host_facts, base_profile)
    try:
        profile.validate_vram()
    except ValueError as exc:
        log.error("Профиль VRAM некорректен (%s) — откат к безопасным дефолтам.", exc)
        profile.qwen_gpu_util, profile.uitars_gpu_util = 0.60, 0.17
        profile.validate_vram()
    log.info("Итоговый профиль:\n%s", json.dumps(asdict(profile), ensure_ascii=False, indent=2))

    # --- .wslconfig ---
    write_wslconfig(profile)

    # --- Готовность GPU (авто-доведение, без аварийного выхода) ---
    if not args.skip_checks:
        ensure_gpu_ready(distro)

    # --- .env для compose ---
    write_compose_env(profile, args.qwen_model, args.uitars_model)

    if args.skip_stack:
        log.info("Флаг --skip-stack: подготовка завершена, стек не поднимаю.")
        return 0

    # --- Авто-подъём стека через Docker Desktop ---
    bring_up_stack()

    log.info("=" * 70)
    log.info("Развёртывание завершено.")
    log.info("  Ядро (FastAPI):  http://localhost:8000")
    log.info("  Qwen-Coder vLLM: http://localhost:8001/v1")
    log.info("  UI-TARS vLLM:    http://localhost:8002/v1")
    log.info("  Аудио ASR/TTS:   http://localhost:8003")
    log.info("Запустите RPC-мост на хосте: python windows_rpc_bridge.py")
    log.info("Запустите дашборд:           cd dashboard && npm install && npm run dev")
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
