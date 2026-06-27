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
import stat
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path, PureWindowsPath
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


def run_streamed(cmd: list[str], *, timeout: int = 600,
                 prefix: str = "    ") -> tuple[int, str]:
    """
    Запустить команду, СТРИМЯ её вывод в реальном времени (чтобы долгие
    операции вроде docker pull не выглядели зависшими). Возвращает (код, текст).
    """
    log.debug("Выполняю (поток): %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines: list[str] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(prefix + line)
            sys.stdout.flush()
            lines.append(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        log.warning("Команда превысила тайм-аут (%d с) и была остановлена.", timeout)
    return proc.returncode or 0, "".join(lines)


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
# Перенос проекта и тяжёлых данных на целевой диск (D:\jarvis)
# --------------------------------------------------------------------------- #
def set_repo_root(root: Path) -> None:
    """Переключить корень проекта (после переноса на другой диск)."""
    global REPO_DIR, COMPOSE_FILE, COMPOSE_ENV
    REPO_DIR = root
    COMPOSE_FILE = root / "wsl" / "docker-compose.agents.yml"
    COMPOSE_ENV = root / "wsl" / ".env"


def _docker_path(p: Path) -> str:
    """Путь в формате, понятном Docker Desktop (прямые слэши)."""
    return str(p).replace("\\", "/")


# Каталоги, которые НЕ переносим (сборочные артефакты, кэши, данные)
RELOCATE_IGNORE_DIRS = {"node_modules", ".next", "out", "__pycache__",
                        "data", ".pytest_cache", ".mypy_cache"}


def _make_writable(path: str) -> None:
    """Снять атрибут «только чтение» (git помечает pack-файлы read-only)."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _force_rmtree(path: Path) -> None:
    """
    Рекурсивно удалить каталог, корректно обрабатывая read-only файлы
    (иначе git pack-файлы на Windows вызывают PermissionError).
    Совместимо с Python ≥3.12 (onexc) и старее (onerror).
    """
    def _handler(func, p, _exc):  # noqa: ANN001
        _make_writable(p)
        try:
            func(p)
        except Exception:  # noqa: BLE001
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_handler)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=lambda f, p, e: _handler(f, p, e))


def robust_copytree(src: Path, dst: Path, *, overwrite: bool = False
                    ) -> tuple[int, int, list[tuple[str, str]]]:
    """
    Надёжно скопировать дерево src → dst:
      • пропускает сборочные каталоги (RELOCATE_IGNORE_DIRS) и *.tar;
      • по умолчанию НЕ ТРОГАЕТ уже существующие на целевом диске файлы
        (overwrite=False) — что и требуется: «если что-то уже живёт на D:, не
        трогаем». Это заодно исключает PermissionError на read-only git-файлах;
      • при overwrite=True снимает read-only и перезаписывает;
      • ошибки отдельных файлов НЕ фатальны — собираются и возвращаются.
    Возвращает (скопировано, пропущено_существующих, список ошибок).
    """
    copied = 0
    skipped = 0
    errors: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(src):
        # Не заходим в игнорируемые подкаталоги
        dirs[:] = [d for d in dirs if d not in RELOCATE_IGNORE_DIRS]
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else dst / rel
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            errors.append((str(target_dir), str(exc)))
            continue
        for name in files:
            if name.endswith(".tar"):
                continue
            s = Path(root) / name
            t = target_dir / name
            if t.exists():
                if not overwrite:
                    skipped += 1          # уже живёт на D: — не трогаем
                    continue
                _make_writable(str(t))
            try:
                shutil.copy2(s, t)
                copied += 1
            except Exception as exc:  # noqa: BLE001
                errors.append((str(s), str(exc)))
    return copied, skipped, errors


class RelocationManager:
    """
    Полный перенос на целевой диск (по умолчанию D:\\jarvis):
      • файлы проекта (репозиторий);
      • тяжёлые данные (модели/кэши) — через bind-mount в <root>\\data;
      • WSL-дистрибутив — export → unregister → import в <root>\\wsl;
      • диск Docker Desktop — смена расположения disk image на <root>\\docker.

    Все опасные шаги защищены: бэкап настроек, экспорт перед unregister,
    пропуск без аварийного выхода при любой ошибке.
    """

    DOCKER_EXE_CANDIDATES = (
        r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
        r"C:\Program Files\Docker\Docker\frontend\Docker Desktop.exe",
    )
    DOCKER_PROCESSES = ("Docker Desktop.exe", "com.docker.backend.exe",
                        "com.docker.service")

    def __init__(self, target_root: Path, *, move_docker: bool,
                 move_distro: bool, delete_source: bool,
                 overwrite: bool = False) -> None:
        self.target_root = target_root
        self.move_docker = move_docker
        self.move_distro = move_distro
        self.delete_source = delete_source
        # overwrite=False → «если что-то уже живёт на D:, не трогаем»
        self.overwrite = overwrite
        self.copy_had_errors = False
        # Перенос имеет смысл только на Windows и при наличии целевого диска
        self.enabled = (platform.system() == "Windows"
                        and os.path.exists((target_root.drive or "C:") + "\\"))
        if not self.enabled:
            log.warning("Перенос на %s пропущен: не Windows или диск %s недоступен.",
                        target_root, target_root.drive)

    # ---- Файлы проекта -------------------------------------------------- #
    def relocate_project(self) -> Optional[Path]:
        """Скопировать проект в target_root и переключить рабочую директорию."""
        if not self.enabled:
            return None
        src = REPO_DIR
        if src.resolve() == self.target_root.resolve():
            log.info("Проект уже находится в %s — перенос файлов не требуется.", self.target_root)
            return None
        log.info("→ Перенос файлов проекта: %s → %s", src, self.target_root)
        self.target_root.mkdir(parents=True, exist_ok=True)
        # Не перезаписываем то, что уже есть на D: (overwrite=self.overwrite).
        copied, skipped, errors = robust_copytree(src, self.target_root, overwrite=self.overwrite)
        log.info("  Скопировано новых файлов: %d; пропущено уже существующих на D:: %d",
                 copied, skipped)
        if errors:
            self.copy_had_errors = True
            log.warning("  Пропущено по ошибке %d файл(ов) (не критично). Пример: %s — %s",
                        len(errors), errors[0][0], errors[0][1])
        os.chdir(self.target_root)
        set_repo_root(self.target_root)
        log.info("  Рабочая директория переключена на %s", self.target_root)
        return src

    def ensure_data_dirs(self) -> Path:
        """Создать каталоги тяжёлых данных на целевом диске."""
        data = self.target_root / "data"
        if not self.enabled:
            return data
        for sub in ("models", "hf", "sandbox"):
            (data / sub).mkdir(parents=True, exist_ok=True)
        log.info("  Каталог тяжёлых данных: %s", data)
        return data

    # ---- Общий перенос WSL-распределения ------------------------------- #
    @staticmethod
    def _wsl_distro_exists(name: str) -> bool:
        _, out, _ = run_wsl_raw(["--list", "--quiet"])
        names = [ln.strip().lower() for ln in out.replace("\r", "").split("\n") if ln.strip()]
        return name.lower() in names

    def _move_distro(self, name: str, dest_dir: Path, tar: Path) -> bool:
        """
        Перенести произвольное WSL-распределение в dest_dir на целевом диске
        (export → unregister → import). Перед unregister делается архив-резерв.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        run_wsl_raw(["--shutdown"])
        rc, out, err = run_wsl_raw(["--export", name, str(tar)], timeout=7200)
        if rc != 0 or not tar.exists() or tar.stat().st_size < 1024:
            log.error("  Экспорт '%s' не удался (%s). Перенос отменён, данные целы.",
                      name, (out + err).strip()[:160])
            return False
        log.info("  Экспортирован '%s': %.0f МБ → %s", name, tar.stat().st_size / 1e6, tar)
        rc, out, err = run_wsl_raw(["--unregister", name], timeout=600)
        if rc != 0:
            log.error("  Не удалось снять регистрацию '%s' (%s). Архив-резерв: %s",
                      name, (out + err).strip()[:160], tar)
            return False
        rc, out, err = run_wsl_raw(
            ["--import", name, str(dest_dir), str(tar), "--version", "2"], timeout=7200)
        if rc != 0:
            log.error("  Импорт '%s' на D: не удался (%s). Восстанавливаю из архива…",
                      name, (out + err).strip()[:160])
            run_wsl_raw(["--import", name, str(dest_dir), str(tar), "--version", "2"], timeout=7200)
            return False
        return True

    # ---- WSL-дистрибутив (Ubuntu) -------------------------------------- #
    def relocate_distro(self, distro: str) -> None:
        if not (self.enabled and self.move_distro and distro):
            return
        marker = self.target_root / "wsl" / ".relocated"
        if marker.exists():
            log.info("WSL-дистрибутив уже перенесён ранее — пропускаю.")
            return
        log.info("→ Перенос WSL-дистрибутива '%s' на D: (export → unregister → import). "
                 "Это может занять время…", distro)
        dest_dir = self.target_root / "wsl" / "distro"
        tar = self.target_root / "wsl" / f"{distro}.tar"
        if not self._move_distro(distro, dest_dir, tar):
            return
        _, out, _ = run_wsl_raw(["-d", distro, "--", "echo", "ok"])
        if "ok" in out:
            marker.write_text("ok", encoding="utf-8")
            log.info("  Дистрибутив '%s' перенесён на D: и работает. Архив-резерв: %s", distro, tar)
        else:
            log.warning("  Перенесённый дистрибутив не отвечает. Архив-резерв сохранён: %s", tar)

    # ---- Диск Docker Desktop ------------------------------------------- #
    def _find_docker_settings(self) -> Optional[Path]:
        appdata = os.environ.get("APPDATA", "")
        for name in ("settings-store.json", "settings.json"):
            p = Path(appdata) / "Docker" / name
            if p.exists():
                return p
        return None

    def _stop_docker(self) -> None:
        for proc in self.DOCKER_PROCESSES:
            run(["taskkill", "/IM", proc, "/F"], check=False)
        run_wsl_raw(["--shutdown"])
        time.sleep(5)

    def _start_docker(self) -> None:
        for exe in self.DOCKER_EXE_CANDIDATES:
            if os.path.exists(exe):
                subprocess.Popen([exe])
                log.info("  Запущен Docker Desktop: %s", exe)
                return
        log.warning("  Не найден исполняемый файл Docker Desktop — запустите его вручную.")

    def _wait_docker_ready(self, retries: int = 30) -> bool:
        for _ in range(retries):
            res = run(["docker", "version", "--format", "{{.Server.Version}}"], check=False)
            if res.returncode == 0 and (res.stdout or "").strip():
                return True
            time.sleep(10)
        return False

    def _set_docker_data_folder(self, settings: Path, target: Path) -> bool:
        """Прописать новое расположение disk image в настройки Docker (с бэкапом)."""
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("  Не удалось прочитать настройки Docker (%s).", exc)
            return False
        backup = settings.with_name(settings.name + f".bak.{int(time.time())}")
        shutil.copy2(settings, backup)
        log.info("  Бэкап настроек Docker: %s", backup)
        # Разные версии Docker Desktop используют разный регистр ключа — пишем оба
        data["dataFolder"] = _docker_path(target)
        data["DataFolder"] = _docker_path(target)
        settings.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("  В настройках задан disk image location: %s", target)
        return True

    def _docker_location_ok(self, settings: Optional[Path], target: Path) -> bool:
        """Подтвердить, что Docker реально использует целевой диск."""
        if settings and settings.exists():
            try:
                data = json.loads(settings.read_text(encoding="utf-8"))
                cur = str(data.get("dataFolder") or data.get("DataFolder") or "")
                if cur and Path(cur).resolve() == target.resolve():
                    return True
            except Exception:  # noqa: BLE001
                pass
        # Признак фактического переноса: в целевой папке появился *.vhdx
        try:
            for sub in ("docker-desktop-data", "docker-desktop", "wsl", "data"):
                p = target / sub
                if p.exists() and any(p.rglob("*.vhdx")):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def relocate_docker(self) -> None:
        if not (self.enabled and self.move_docker):
            return
        target = self.target_root / "docker"
        target.mkdir(parents=True, exist_ok=True)
        settings = self._find_docker_settings()

        if self._docker_location_ok(settings, target):
            log.info("Диск Docker Desktop уже на D: — пропускаю.")
            return

        log.info("→ Перенос Docker Desktop на %s. Docker будет остановлен и перезапущен…", target)
        if settings is None:
            log.warning("  Файл настроек Docker Desktop не найден — полагаюсь на перенос "
                        "распределения данных docker-desktop-data.")

        # 1) Остановить Docker полностью
        self._stop_docker()

        # 2) Прописать disk image location в настройках (консолидированная схема Docker)
        if settings is not None:
            self._set_docker_data_folder(settings, target)

        # 3) ФАКТИЧЕСКИЙ перенос распределения образов docker-desktop-data
        #    (классическая схема WSL2-бэкенда). Это гарантированно кладёт VHDX
        #    с образами на D:.
        if self._wsl_distro_exists("docker-desktop-data"):
            log.info("  Обнаружено распределение docker-desktop-data — переношу его на D:…")
            dest = target / "docker-desktop-data"
            tar = target / "docker-desktop-data.tar"
            if self._move_distro("docker-desktop-data", dest, tar):
                log.info("  ✔ docker-desktop-data (образы/тома) перенесён в %s", dest)
        else:
            log.info("  docker-desktop-data не найден (новая консолидированная схема) — "
                     "перенос обеспечивается настройкой disk image location.")

        # 4) Запуск Docker и проверка
        self._start_docker()
        if not self._wait_docker_ready():
            log.warning("  Docker Desktop не ответил вовремя после переноса.")

        # 5) Верификация результата
        if self._docker_location_ok(settings, target):
            log.info("  ✔ ГОТОВО: данные/образы Docker Desktop теперь на D: (%s).", target)
        else:
            log.warning("  ✗ Автоматически ПОДТВЕРДИТЬ перенос Docker на D: не удалось "
                        "(ваша версия Docker Desktop хранит путь под другим ключом). "
                        "Доделайте в GUI: Docker Desktop → Settings → Resources → Advanced → "
                        "Disk image location = %s → Apply & Restart. После этого образы будут на D:.",
                        target)

    # ---- Префлайт: фактическое расположение данных Docker -------------- #
    @staticmethod
    def _docker_data_location() -> Optional[Path]:
        """
        Определить РЕАЛЬНОЕ расположение данных Docker через реестр WSL
        (HKCU\\...\\Lxss → BasePath дистрибутивов docker-desktop-data/docker-desktop).
        Версионно-независимо. Возвращает путь или None, если определить не удалось.
        """
        try:
            import winreg  # доступен только на Windows
        except ImportError:
            return None
        lxss = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
        found: dict[str, str] = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, lxss) as root:
                idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, idx)
                    except OSError:
                        break
                    idx += 1
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            name = winreg.QueryValueEx(k, "DistributionName")[0]
                            if name in ("docker-desktop-data", "docker-desktop"):
                                found[name] = winreg.QueryValueEx(k, "BasePath")[0]
                    except OSError:
                        continue
        except OSError:
            return None
        for name in ("docker-desktop-data", "docker-desktop"):
            if name in found:
                bp = found[name]
                if bp.startswith("\\\\?\\"):   # снимаем префикс расширенного пути
                    bp = bp[4:]
                return Path(bp)
        return None

    def verify_docker_on_target(self) -> Optional[bool]:
        """
        True  — данные Docker точно на целевом диске;
        False — точно на ДРУГОМ диске (например, C:);
        None  — определить не удалось.
        """
        target = self.target_root / "docker"
        loc = self._docker_data_location()
        # Сравнение дисков — через PureWindowsPath (не зависит от ОС запуска)
        target_drive = PureWindowsPath(str(self.target_root)).drive.upper()
        if loc is not None:
            loc_str = str(loc)
            if loc_str.startswith("\\\\?\\"):   # снимаем префикс расширенного пути
                loc_str = loc_str[4:]
            loc_drive = PureWindowsPath(loc_str).drive.upper()
            log.info("  Данные Docker сейчас: %s (диск %s)", loc, loc_drive or "?")
            return loc_drive == target_drive
        # Вторичный признак — наличие vhdx в целевой папке или ключ настроек
        if self._docker_location_ok(self._find_docker_settings(), target):
            return True
        return None

    # ---- Удаление исходной папки --------------------------------------- #
    def cleanup_source(self, old: Optional[Path]) -> None:
        if not (self.enabled and self.delete_source and old):
            return
        if old.resolve() == self.target_root.resolve():
            return
        if self.copy_had_errors:
            log.warning("Исходная папка %s НЕ удалена: при копировании были ошибки — "
                        "сохраняю оригинал во избежание потери данных.", old)
            return
        try:
            _force_rmtree(old)  # корректно удаляет read-only файлы git
            log.info("Исходная папка проекта удалена: %s", old)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось удалить исходную папку %s (%s). Удалите вручную позже.", old, exc)


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

    # Предпочтительные модели для генерации конфигурации (по убыванию).
    # Gemma — первой, чтобы не заставлять LM Studio переключать уже загруженную
    # пользователем модель. С json_schema и большим max_tokens reasoning-модель
    # (gemma-4-*-qat) корректно успевает выдать JSON после рассуждений.
    # Если gemma недоступна — берём instruct-модели без «думанья».
    PREFERRED = ("gemma-4-31b-it-qat", "gemma-4-26b-a4b-it-qat",
                 "qwen3-coder-30b-a3b-instruct", "devstral-small-2-24b-instruct-2512")

    # JSON-схема профиля. ВНИМАНИЕ: LM Studio поддерживает только
    # response_format.type ∈ {"json_schema","text"} (а НЕ "json_object").
    PROFILE_SCHEMA: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": "deployment_profile",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "wsl_memory_gb": {"type": "integer"},
                    "wsl_processors": {"type": "integer"},
                    "wsl_swap_gb": {"type": "integer"},
                    "qwen_gpu_util": {"type": "number"},
                    "qwen_max_model_len": {"type": "integer"},
                    "uitars_gpu_util": {"type": "number"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["wsl_memory_gb", "wsl_processors", "wsl_swap_gb",
                             "qwen_gpu_util", "qwen_max_model_len",
                             "uitars_gpu_util", "notes"],
            },
        },
    }

    # Большой лимит токенов: reasoning-модели (gemma-qat) тратят 800+ токенов
    # только на рассуждения, поэтому даём запас под reasoning + сам JSON.
    MAX_TOKENS = 4096

    def __init__(self, base_url: str, model: str = "", timeout: int = 180) -> None:
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

    def _post(self, model: str, messages: list[dict],
              response_format: Optional[dict] = None) -> requests.Response:
        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "temperature": 0.1, "max_tokens": self.MAX_TOKENS}
        if response_format is not None:
            payload["response_format"] = response_format
        return requests.post(f"{self.base_url}/chat/completions", json=payload,
                             timeout=self.timeout)

    def generate_profile(self, host_facts: dict[str, Any],
                         base: DeploymentProfile) -> DeploymentProfile:
        model = self._select_model()
        log.info("Опрашиваю LM Studio (модель: %s)…", model)

        system_prompt = textwrap.dedent("""\
            Ты — инженер по развёртыванию JARVIS-OS на Windows 11 (WSL2 + vLLM +
            Docker) с GPU NVIDIA RTX 5090 (32 ГБ VRAM). По фактам о хосте верни
            ТОЛЬКО валидный JSON-объект (без markdown, без пояснений, без
            пошаговых рассуждений — сразу результат) с ключами:
            wsl_memory_gb (int), wsl_processors (int), wsl_swap_gb (int),
            qwen_gpu_util (float 0..1), qwen_max_model_len (int),
            uitars_gpu_util (float 0..1), notes (массив строк на русском).
            Зарезервируй ~5.5 ГБ VRAM под хост/Xvfb/KV-кэш; сумма
            (qwen_gpu_util + uitars_gpu_util) * 32 + 2 не должна превышать 26.5.
        """)
        user_prompt = "Факты о хосте (JSON):\n" + json.dumps(host_facts, ensure_ascii=False, indent=2)
        sys_msg = {"role": "system", "content": system_prompt}
        usr_msg = {"role": "user", "content": user_prompt}
        # Слитный вариант на случай моделей без поддержки роли system
        merged_msg = {"role": "user", "content": system_prompt + "\n\n" + user_prompt}

        # Прогрессивные попытки. Первая — json_schema (корректный для LM Studio
        # формат структурированного вывода), она же самая надёжная.
        attempts = [
            ("json_schema (строгая схема)", [sys_msg, usr_msg], self.PROFILE_SCHEMA),
            ("text (без схемы)", [sys_msg, usr_msg], {"type": "text"}),
            ("без response_format, слитный prompt", [merged_msg], None),
        ]
        for label, messages, response_format in attempts:
            try:
                r = self._post(model, messages, response_format)
                if r.status_code in (400, 422):
                    log.warning("LM Studio %s на варианте «%s»: %s — пробую следующий.",
                                r.status_code, label, (r.text or "")[:160])
                    continue
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                # Reasoning-модели кладут текст в content; если он пуст —
                # пробуем reasoning_content (модель «думала», но не финализировала).
                content = (msg.get("content") or "").strip()
                if not content:
                    content = (msg.get("reasoning_content") or "").strip()
                    if content:
                        log.info("  content пуст — извлекаю JSON из reasoning_content.")
                data = self._extract_json(content)
                merged = self._merge_profile(base, data)
                log.info("LM Studio успешно сгенерировал конфигурацию (вариант «%s»).", label)
                merged.notes.append(f"Конфигурация получена от модели {model}.")
                return merged
            except Exception as exc:  # noqa: BLE001
                log.warning("Вариант «%s» не удался (%s) — продолжаю.", label, exc)
                continue

        log.warning("LM Studio не дал валидного ответа ни в одном варианте. "
                    "Использую безопасный дефолтный профиль (он корректен для RTX 5090).")
        base.notes.append("Профиль сгенерирован дефолтом: LM Studio не ответил корректно.")
        return base

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            raise ValueError("Пустой ответ модели (нет ни content, ни reasoning_content).")
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
        if fence:
            content = fence.group(1)
        else:
            # Берём ПОСЛЕДНИЙ JSON-объект (у reasoning-моделей финальный ответ — в конце)
            braces = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content, re.DOTALL)
            if braces:
                content = braces[-1]
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
    log.info("→ Тест GPU в контейнере (хостовый Docker). "
             "Идёт одноразовая загрузка тестового образа (~150 МБ), подождите…")
    # docker run сам докачает образ при отсутствии; вывод стримим, чтобы было
    # видно прогресс загрузки и операция не выглядела зависшей.
    rc, out = run_streamed(
        ["docker", "run", "--rm", "--gpus", "all", GPU_TEST_IMAGE, "nvidia-smi", "-L"],
        timeout=900,
    )
    if rc == 0 and "GPU" in out:
        log.info("  GPU доступен контейнерам.")
        return True
    log.warning("  GPU пока недоступен контейнерам (код %s): %s", rc, out.strip()[:200])
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
def write_compose_env(profile: DeploymentProfile, qwen_model: str, uitars_model: str,
                      data_dir: Path) -> Path:
    """Сформировать wsl/.env для интерполяции docker compose."""
    JARVIS_HOME.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    # Docker Desktop принимает Windows-путь с прямыми слэшами
    home_host = str(JARVIS_HOME).replace("\\", "/")
    data_host = _docker_path(data_dir)
    env = textwrap.dedent(f"""\
        # Сгенерировано bootstrap_installer.py — переменные для docker compose
        JARVIS_QWEN_MODEL={qwen_model}
        JARVIS_UITARS_MODEL={uitars_model}
        JARVIS_QWEN_GPU_UTIL={profile.qwen_gpu_util}
        JARVIS_QWEN_MAX_LEN={profile.qwen_max_model_len}
        JARVIS_UITARS_GPU_UTIL={profile.uitars_gpu_util}
        JARVIS_HOME_HOST={home_host}
        # Тяжёлые данные (веса моделей, кэш HF, sandbox) живут на целевом диске:
        JARVIS_DATA_DIR={data_host}
        HF_TOKEN={os.environ.get('HF_TOKEN', '')}
        WHISPER_MODEL=large-v3
        WHISPER_COMPUTE_TYPE=int8_float16
        KOKORO_VOICE=af_sky
    """)
    COMPOSE_ENV.write_text(env, encoding="utf-8")
    log.info("  Записан %s (данные → %s)", COMPOSE_ENV, data_host)
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


def prefetch_models(data_dir: Path, qwen_model: str, uitars_model: str,
                    retries: int = 6) -> None:
    """
    ВОЗОБНОВЛЯЕМАЯ предзагрузка весов моделей в персистентный кэш HF на целевом
    диске (data_dir/hf). hf_transfer выключен → при обрыве докачка продолжается
    с места обрыва, а не с нуля. Идемпотентно: уже скачанные файлы пропускаются.
    vLLM затем находит модели в кэше и не качает повторно.
    """
    hf_cache = _docker_path(data_dir / "hf")
    (data_dir / "hf").mkdir(parents=True, exist_ok=True)
    models = [m for m in (qwen_model, uitars_model) if m]
    log.info("→ Предзагрузка весов моделей в кэш %s (возобновляемо, с ретраями)…", hf_cache)
    for model in models:
        done = False
        for attempt in range(1, retries + 1):
            log.info("  Модель %s — попытка %d/%d…", model, attempt, retries)
            inner = (
                "pip install -q --no-cache-dir 'huggingface_hub[cli]>=0.24' && "
                f"huggingface-cli download {model}"
            )
            rc, _ = run_streamed([
                "docker", "run", "--rm",
                "-e", "HF_HUB_ENABLE_HF_TRANSFER=0",
                "-e", "HF_HUB_DOWNLOAD_TIMEOUT=120",
                "-e", f"HF_TOKEN={os.environ.get('HF_TOKEN', '')}",
                "-e", f"HUGGING_FACE_HUB_TOKEN={os.environ.get('HF_TOKEN', '')}",
                "-v", f"{hf_cache}:/root/.cache/huggingface",
                "python:3.11-slim", "bash", "-lc", inner,
            ], timeout=36000)
            if rc == 0:
                done = True
                break
            wait = min(2 ** attempt, 30)
            log.warning("  Загрузка %s прервана (код %s). Возобновлю через %d с "
                        "(уже скачанное не теряется)…", model, rc, wait)
            time.sleep(wait)
        if done:
            log.info("  ✔ %s — в кэше на D:.", model)
        else:
            log.warning("  ✗ %s не докачана за %d попыток — vLLM попробует докачать сам "
                        "при старте (тоже с возобновлением).", model, retries)


def _compose_with_retries(action: list[str], retries: int = 4) -> int:
    """Выполнить compose-операцию с ретраями (для устойчивости к обрывам сети)."""
    rc = 0
    for attempt in range(1, retries + 1):
        rc = compose(*action, stream=True)
        if rc == 0:
            return 0
        wait = min(2 ** attempt, 30)
        log.warning("  docker compose %s — код %s (попытка %d/%d). Повтор через %d с "
                    "(готовые слои Docker не качаются заново)…",
                    " ".join(action), rc, attempt, retries, wait)
        time.sleep(wait)
    return rc


def bring_up_stack() -> None:
    """Автоматически поднять весь контейнерный стек через Docker Desktop."""
    log.info("→ Загрузка образов (docker compose pull) с ретраями…")
    _compose_with_retries(["pull", "--ignore-pull-failures"])
    log.info("→ Сборка образов (docker compose build) с ретраями…")
    _compose_with_retries(["build"])
    log.info("→ Запуск стека (docker compose up -d)…")
    _compose_with_retries(["up", "-d", "--remove-orphans"])

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
    p.add_argument("--model", default="", help="ID модели LM Studio (для --use-lmstudio).")
    p.add_argument("--use-lmstudio", action="store_true",
                   help="Опросить LM Studio для генерации профиля. ПО УМОЛЧАНИЮ ВЫКЛ: чтобы "
                        "не загружать вторую модель (иначе вместе с моделью агента это "
                        "вызывает OOM на 32 ГБ GPU). Требует явного --model.")
    p.add_argument("--distro", default="", help="Имя дистрибутива WSL (по умолчанию — авто).")
    p.add_argument("--wsl-ram", type=int, default=96)
    p.add_argument("--wsl-cpus", type=int, default=20)
    p.add_argument("--qwen-model", default="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ")
    p.add_argument("--uitars-model", default="bytedance-research/UI-TARS-7B-DPO")
    p.add_argument("--target-root", default=r"D:\jarvis",
                   help="Корневой путь переноса проекта и тяжёлых данных.")
    p.add_argument("--no-relocate", action="store_true",
                   help="Не переносить проект/данные на целевой диск.")
    p.add_argument("--no-move-docker", action="store_true",
                   help="Не переносить расположение диска Docker Desktop.")
    p.add_argument("--allow-docker-on-c", action="store_true",
                   help="Разрешить подъём стека, даже если данные Docker не на целевом "
                        "диске (по умолчанию bootstrap остановится, чтобы образы не "
                        "утекли на C:).")
    p.add_argument("--no-move-distro", action="store_true",
                   help="Не переносить WSL-дистрибутив на целевой диск.")
    p.add_argument("--keep-source", action="store_true",
                   help="Не удалять исходную папку проекта после переноса.")
    p.add_argument("--overwrite-existing", action="store_true",
                   help="Перезаписывать файлы, уже существующие на целевом диске "
                        "(по умолчанию существующее на D: не трогается).")
    p.add_argument("--no-auto-install", action="store_true",
                   help="Не устанавливать дистрибутив WSL автоматически.")
    p.add_argument("--skip-stack", action="store_true",
                   help="Только подготовка (без подъёма контейнеров).")
    p.add_argument("--skip-prefetch", action="store_true",
                   help="Не предзагружать веса моделей (vLLM скачает сам при старте).")
    p.add_argument("--skip-gpu-check", action="store_true",
                   help="Пропустить тест GPU в контейнере (если он долгий/не нужен).")
    p.add_argument("--skip-checks", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log.info("=" * 70)
    log.info("JARVIS-OS · bootstrap_installer · полностью автоматическое развёртывание")
    log.info("=" * 70)

    # --- ФАЗА 0: перенос файлов проекта на целевой диск (D:\jarvis) ---
    relocator: Optional[RelocationManager] = None
    old_source: Optional[Path] = None
    if not args.no_relocate:
        relocator = RelocationManager(
            Path(args.target_root),
            move_docker=not args.no_move_docker,
            move_distro=not args.no_move_distro,
            delete_source=not args.keep_source,
            overwrite=args.overwrite_existing,
        )
        try:
            old_source = relocator.relocate_project()
            relocator.ensure_data_dirs()
        except Exception as exc:  # noqa: BLE001
            log.warning("Перенос файлов проекта прерван ошибкой (%s). "
                        "Продолжаю работу на текущем расположении.", exc)
            old_source = None

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

    # --- ФАЗА переноса тяжёлых частей на D: (WSL-дистрибутив + диск Docker) ---
    if relocator:
        try:
            relocator.relocate_distro(distro or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("Перенос WSL-дистрибутива пропущен (%s).", exc)
        try:
            relocator.relocate_docker()
        except Exception as exc:  # noqa: BLE001
            log.warning("Перенос диска Docker Desktop пропущен (%s).", exc)

    # --- Профиль развёртывания ---
    # ВАЖНО: по умолчанию bootstrap НЕ обращается к LM Studio и НЕ загружает
    # никакую модель — иначе вместе с моделью агента-установщика это вызывает OOM
    # на 32 ГБ GPU. Профиль детерминированный и корректен для RTX 5090.
    base_profile = DeploymentProfile(wsl_memory_gb=args.wsl_ram,
                                     wsl_processors=args.wsl_cpus,
                                     wsl_distro=distro or "")
    if args.use_lmstudio and args.model:
        host_facts = {
            "os": platform.platform(),
            "cpu": platform.processor() or "Intel Core Ultra 9 285K",
            "logical_cpus": os.cpu_count(),
            "target_gpu": "NVIDIA RTX 5090 32GB",
            "total_ram_gb": 128,
            "requested_wsl_ram_gb": args.wsl_ram,
            "requested_wsl_cpus": args.wsl_cpus,
        }
        log.info("Запрашиваю профиль у LM Studio (модель %s, по явному --use-lmstudio).", args.model)
        profile = LMStudioClient(args.lmstudio, model=args.model).generate_profile(host_facts, base_profile)
    else:
        if args.use_lmstudio and not args.model:
            log.warning("--use-lmstudio без --model: чтобы НЕ загрузить вторую модель, "
                        "пропускаю запрос к LM Studio.")
        log.info("Профиль развёртывания: детерминированный дефолт (без обращения к LLM).")
        profile = base_profile
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
    if not args.skip_checks and not args.skip_gpu_check:
        ensure_gpu_ready(distro)
    elif args.skip_gpu_check:
        log.info("Тест GPU пропущен по флагу --skip-gpu-check.")

    # --- .env для compose (тяжёлые данные → целевой диск) ---
    data_dir = (relocator.target_root / "data") if (relocator and relocator.enabled) \
        else (REPO_DIR / "data")
    write_compose_env(profile, args.qwen_model, args.uitars_model, data_dir)

    if args.skip_stack:
        log.info("Флаг --skip-stack: подготовка завершена, стек не поднимаю.")
        if relocator:
            relocator.cleanup_source(old_source)
        return 0

    # --- ПРЕФЛАЙТ: не дать образам (~10+ ГБ) утечь на C: ---
    if (relocator and relocator.enabled and relocator.move_docker
            and not args.allow_docker_on_c):
        status = relocator.verify_docker_on_target()
        drive = relocator.target_root.drive or "D:"
        if status is False:
            log.error("=" * 70)
            log.error("СТОП: данные Docker всё ещё НЕ на диске %s.", drive)
            log.error("Если продолжить, образы Docker (~10+ ГБ) будут скачаны НЕ на %s.", drive)
            log.error("Как исправить: Docker Desktop → Settings → Resources → Advanced →")
            log.error("  Disk image location = %s\\docker → Apply & Restart, затем повторите запуск.",
                      relocator.target_root)
            log.error("Либо запустите осознанно с флагом --allow-docker-on-c.")
            log.error("=" * 70)
            return 4
        if status is None:
            log.warning("Не удалось ПОДТВЕРДИТЬ расположение данных Docker. Если вы не "
                        "переключали Disk image location на %s, образы могут уйти на C:. "
                        "Продолжаю (отключить проверку: --allow-docker-on-c).", drive)
        else:
            log.info("✔ Префлайт: данные Docker на диске %s — продолжаю подъём стека.", drive)

    # --- Возобновляемая предзагрузка весов моделей (в кэш на D:) ---
    if not args.skip_prefetch:
        prefetch_models(data_dir, args.qwen_model, args.uitars_model)

    # --- Авто-подъём стека через Docker Desktop ---
    bring_up_stack()

    # --- Удаление исходной папки на C: (полный перенос) ---
    if relocator:
        relocator.cleanup_source(old_source)

    log.info("=" * 70)
    log.info("Развёртывание завершено.")
    log.info("  Корень проекта:  %s", REPO_DIR)
    log.info("  Тяжёлые данные:  %s", data_dir)
    log.info("  Ядро (FastAPI):  http://localhost:8000")
    log.info("  Qwen-Coder vLLM: http://localhost:8001/v1")
    log.info("  UI-TARS vLLM:    http://localhost:8002/v1")
    log.info("  Аудио ASR/TTS:   http://localhost:8003")
    if relocator and relocator.enabled:
        log.info("ВПРЕДЬ запускайте всё из %s (например: cd /d %s)",
                 REPO_DIR, REPO_DIR)
    log.info("Запустите RPC-мост на хосте: python windows_rpc_bridge.py")
    log.info("Запустите дашборд:           cd dashboard && npm install && npm run dev")
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("Прервано пользователем (Ctrl+C). Незавершённые шаги можно "
                    "продолжить повторным запуском — скрипт идемпотентен.")
        sys.exit(130)
