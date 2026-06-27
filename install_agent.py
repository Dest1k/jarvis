#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_agent.py — АВТОНОМНЫЙ агент-установщик JARVIS-OS на базе локальной LLM.

Локальная модель (LM Studio, OpenAI-совместимый API) сама ведёт весь процесс
развёртывания: анализирует состояние хоста, принимает решения, исполняет
команды (PowerShell / cmd / WSL / Docker / файловые операции / сеть), вызывает
проверенный bootstrap_installer.py, диагностирует ошибки и восстанавливается —
БЕЗ вмешательства пользователя.

Архитектура: цикл ReAct поверх локальной модели.
  на каждом шаге модель возвращает JSON {thought, action, args};
  агент исполняет инструмент, возвращает наблюдение; цикл продолжается до finish.

БЕЗОПАСНОСТЬ (в интересах пользователя — защита от галлюцинаций модели):
  • КАТАСТРОФИЧЕСКИЕ команды (формат диска, diskpart clean, rm -rf корня, mkfs,
    удаление системного реестра, bcdedit и т.п.) — ЖЁСТКО блокируются ВСЕГДА.
  • Деструктивные команды (удаление, taskkill, git push, сетевые) — в авто-режиме
    выполняются сами; флаг --require-approval переводит их в подтверждение.
  • Ограничение числа шагов, полный транскрипт в лог, режим --dry-run.

Запуск (PowerShell на хосте Windows):
    python install_agent.py --lmstudio http://localhost:1234/v1 --target-root D:\jarvis

Зависимость: requests (pip install requests).
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
import time
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    print("[ОШИБКА] Требуется пакет 'requests'. Установите: pip install requests")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Журналирование (консоль + файл-транскрипт)
# --------------------------------------------------------------------------- #
log = logging.getLogger("jarvis.agent")
log.setLevel(logging.INFO)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | AGENT | %(message)s",
                                        datefmt="%H:%M:%S"))
log.addHandler(_console)


# --------------------------------------------------------------------------- #
# Стоп-листы безопасности
# --------------------------------------------------------------------------- #
# КАТАСТРОФА — необратимое разрушение диска/ОС. Блокируется ВСЕГДА, без исключений.
CATASTROPHIC_PATTERNS = (
    r"\bformat\b\s+[a-z]:", r"\bformat\s+/", r"\bmkfs\b", r"\bdiskpart\b",
    r"\bclean\b\s+all", r"\bfdisk\b", r"\bwipefs\b",
    r"rm\s+-rf?\s+/(\s|\*|$)",          # удаление корня /
    r"rm\s+-rf?\s+/\*",                 # rm -rf /*
    r"rm\s+-rf?\s+~(/|\s|$)",           # удаление домашнего каталога
    r"rm\s+-rf?\s+\$home\b",
    r"rm\s+-rf?\s+/mnt/[a-z]\b",        # удаление диска Windows из WSL
    r"rm\s+-rf?\s+--no-preserve-root",
    r"del\s+/[sq].*\b[a-z]:\\\s*$", r"\brd\s+/s\s+/q\s+[a-z]:\\",
    r"remove-item.*-recurse.*[a-z]:\\\s*$",
    r"reg\s+delete\s+hk(lm|ey_local_machine)\\system",
    r"\bbcdedit\b", r"\bvssadmin\s+delete\b", r"cipher\s+/w",
    r">\s*/dev/sd[a-z]", r"dd\s+if=.*of=/dev/sd[a-z]",
    r"shutdown\s+/r.*/f.*/t\s*0",  # мгновенная принудительная перезагрузка
)

# ДЕСТРУКТИВНО — требует подтверждения при --require-approval, иначе авто.
DESTRUCTIVE_MARKERS = (
    "rm ", "rmdir", "del ", "remove-item", "rd /s", "unregister",
    "shutdown", "reboot", "restart-computer", "stop-computer",
    "git push", "git reset --hard", "git clean", "taskkill", "kill ",
    "stop-process", "net stop", "sc delete", "reg delete",
    "uninstall", "prune", "docker rm", "docker rmi", "wsl --unregister",
)


def is_catastrophic(command: str) -> bool:
    low = command.lower()
    return any(re.search(p, low) for p in CATASTROPHIC_PATTERNS)


def is_destructive(command: str) -> bool:
    low = command.lower()
    return any(m in low for m in DESTRUCTIVE_MARKERS)


# --------------------------------------------------------------------------- #
# Инструменты хоста (то, что агент может делать)
# --------------------------------------------------------------------------- #
MAX_OBS = 6000   # максимум символов наблюдения, передаваемого модели


def _truncate(text: str, limit: int = MAX_OBS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}\n…[обрезано {len(text) - limit} символов]…\n{tail}"


class HostTools:
    """Реализация инструментов, доступных агенту."""

    def __init__(self, target_root: Path, repo_dir: Path, dry_run: bool = False) -> None:
        self.target_root = target_root
        self.repo_dir = repo_dir
        self.dry_run = dry_run

    # -- низкоуровневый запуск -------------------------------------------- #
    def _run(self, cmd: list[str] | str, *, shell: bool = False,
             timeout: int = 1800, cwd: Optional[str] = None) -> dict[str, Any]:
        if self.dry_run:
            return {"returncode": 0, "output": f"[dry-run] не исполнено: {cmd}"}
        try:
            proc = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
            out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
            return {"returncode": proc.returncode, "output": _truncate(out)}
        except subprocess.TimeoutExpired:
            return {"returncode": 124, "output": f"Тайм-аут ({timeout} с): {cmd}"}
        except Exception as exc:  # noqa: BLE001
            return {"returncode": 1, "output": f"Ошибка запуска: {exc}"}

    # -- инструменты ------------------------------------------------------ #
    def run_powershell(self, command: str = "", **_: Any) -> dict[str, Any]:
        return self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command])

    def run_cmd(self, command: str = "", **_: Any) -> dict[str, Any]:
        return self._run(command, shell=True)

    def run_wsl(self, command: str = "", distro: str = "", **_: Any) -> dict[str, Any]:
        args = ["wsl"]
        if distro:
            args += ["-d", distro]
        args += ["-u", "root", "--", "bash", "-lc", command]
        return self._run(args)

    def docker(self, args: str = "", **_: Any) -> dict[str, Any]:
        return self._run("docker " + args, shell=True, timeout=3600)

    def read_file(self, path: str = "", **_: Any) -> dict[str, Any]:
        try:
            return {"returncode": 0, "output": _truncate(Path(path).read_text(encoding="utf-8", errors="replace"))}
        except Exception as exc:  # noqa: BLE001
            return {"returncode": 1, "output": f"Не прочитать {path}: {exc}"}

    def write_file(self, path: str = "", content: str = "", **_: Any) -> dict[str, Any]:
        if self.dry_run:
            return {"returncode": 0, "output": f"[dry-run] запись в {path} ({len(content)} симв.)"}
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return {"returncode": 0, "output": f"Записано {len(content)} символов в {path}"}
        except Exception as exc:  # noqa: BLE001
            return {"returncode": 1, "output": f"Не записать {path}: {exc}"}

    def http_get(self, url: str = "", **_: Any) -> dict[str, Any]:
        try:
            r = requests.get(url, timeout=15)
            return {"returncode": 0, "output": _truncate(f"HTTP {r.status_code}\n{r.text}")}
        except Exception as exc:  # noqa: BLE001
            return {"returncode": 1, "output": f"Запрос не удался: {exc}"}

    def start_background(self, command: str = "", name: str = "jarvis",
                         cwd: str = "", **_: Any) -> dict[str, Any]:
        """
        Запустить ФОНОВЫЙ долгоживущий процесс (демон), который продолжит работать
        после завершения агента: RPC-мост на хосте, dev-сервер дашборда и т.п.
        Открывается в отдельном окне консоли (переживает завершение агента).
        """
        if self.dry_run:
            return {"returncode": 0, "output": f"[dry-run] фон: {command} (cwd={cwd or self.repo_dir})"}
        workdir = cwd or str(self.repo_dir)
        try:
            subprocess.Popen(f'start "{name}" cmd /k {command}', shell=True, cwd=workdir)
            return {"returncode": 0, "output": f"Фоновый процесс запущен (cwd={workdir}): {command}"}
        except Exception as exc:  # noqa: BLE001
            return {"returncode": 1, "output": f"Не запустить фоновый процесс: {exc}"}

    def check_endpoints(self, **_: Any) -> dict[str, Any]:
        """Проверить ВСЕ сервисы целевой архитектуры (критерий готовности)."""
        import socket
        results: dict[str, str] = {}
        http_eps = {
            "backend(8000)": "http://127.0.0.1:8000/health",
            "vllm_qwen(8001)": "http://127.0.0.1:8001/health",
            "vllm_uitars(8002)": "http://127.0.0.1:8002/health",
            "audio(8003)": "http://127.0.0.1:8003/health",
            "dashboard(3000)": "http://127.0.0.1:3000",
        }
        for name, url in http_eps.items():
            try:
                r = requests.get(url, timeout=4)
                results[name] = f"OK {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                results[name] = f"НЕТ ({exc.__class__.__name__})"
        try:
            s = socket.create_connection(("127.0.0.1", 8765), timeout=4)
            s.close()
            results["rpc_bridge(8765)"] = "OK (порт слушает)"
        except Exception:  # noqa: BLE001
            results["rpc_bridge(8765)"] = "НЕТ (8765 не слушает)"
        all_ok = all(v.startswith("OK") for v in results.values())
        results["_ГОТОВО_ПОЛНОСТЬЮ"] = "ДА" if all_ok else "НЕТ"
        return {"returncode": 0, "output": json.dumps(results, ensure_ascii=False, indent=2)}

    def run_bootstrap(self, args: str = "", **_: Any) -> dict[str, Any]:
        """
        Запустить проверенный bootstrap_installer.py (целиком или частями).
        Это «рычаг» — делегирование оркестрации готовому ДЕТЕРМИНИРОВАННОМУ коду.

        ЕДИНАЯ МОДЕЛЬ: bootstrap по умолчанию НЕ обращается к LLM. На всякий
        случай жёстко вырезаем любые флаги, которые могли бы загрузить ВТОРУЮ
        модель (--use-lmstudio / --model / --lmstudio) — защита от OOM.
        """
        script = str(self.repo_dir / "bootstrap_installer.py")
        banned_with_value = {"--model", "--lmstudio"}
        cleaned: list[str] = []
        skip_next = False
        for tok in args.split():
            if skip_next:
                skip_next = False
                continue
            if tok == "--use-lmstudio":
                continue
            if tok in banned_with_value:
                skip_next = True
                continue
            if tok.startswith("--model=") or tok.startswith("--lmstudio="):
                continue
            cleaned.append(tok)
        cmd = [sys.executable, script] + cleaned
        return self._run(cmd, timeout=36000)

    def get_state(self, **_: Any) -> dict[str, Any]:
        """Собрать снимок состояния хоста для принятия решений моделью."""
        state: dict[str, Any] = {
            "os": platform.platform(),
            "logical_cpus": os.cpu_count(),
            "python": sys.version.split()[0],
            "target_root": str(self.target_root),
            "target_exists": self.target_root.exists(),
        }

        def cap(cmd, shell=False):
            r = self._run(cmd, shell=shell, timeout=60)
            return r["output"].strip()

        state["nvidia_smi"] = cap(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                   "--format=csv,noheader"]) if shutil.which("nvidia-smi") else "нет"
        state["docker_version"] = cap("docker version --format '{{.Server.Version}}'", shell=True) \
            if shutil.which("docker") else "нет"
        state["docker_info_os"] = cap("docker info --format '{{.OperatingSystem}}'", shell=True) \
            if shutil.which("docker") else "нет"
        state["wsl_distros"] = cap("wsl --list --verbose", shell=True) if shutil.which("wsl") else "нет"
        # Свободное место на целевом диске
        try:
            drive = (self.target_root.drive or "C:") + "\\"
            total, used, free = shutil.disk_usage(drive)
            state["target_free_gb"] = round(free / 1e9, 1)
        except Exception:  # noqa: BLE001
            state["target_free_gb"] = "?"
        # Кеш моделей
        hf = self.target_root / "data" / "hf"
        state["hf_cache_exists"] = hf.exists()
        # Статус контейнеров
        compose = self.repo_dir / "wsl" / "docker-compose.agents.yml"
        if compose.exists() and shutil.which("docker"):
            state["compose_ps"] = cap(f'docker compose -f "{compose}" ps', shell=True)
        return {"returncode": 0, "output": json.dumps(state, ensure_ascii=False, indent=2)}


# Описание инструментов для модели
TOOLS_DOC = {
    "get_state": "Снимок состояния хоста (ОС, GPU, Docker, WSL, свободное место). args: {}",
    "run_powershell": "Выполнить PowerShell. args: {command}",
    "run_cmd": "Выполнить cmd.exe. args: {command}",
    "run_wsl": "Выполнить bash в WSL (root). args: {command, distro?}",
    "docker": "Выполнить docker-команду. args: {args}  (например 'compose -f wsl/docker-compose.agents.yml ps')",
    "read_file": "Прочитать файл. args: {path}",
    "write_file": "Записать файл. args: {path, content}",
    "http_get": "HTTP GET (проверка эндпоинтов/health). args: {url}",
    "start_background": "Запустить ФОНОВЫЙ демон (переживает агента): RPC-мост, "
                        "dev-сервер дашборда. args: {command, name?, cwd?}",
    "check_endpoints": "Проверить ВСЕ сервисы целевой архитектуры разом "
                       "(8000-8003 + dashboard 3000 + rpc_bridge 8765). args: {}",
    "run_bootstrap": "Запустить проверенный bootstrap_installer.py (детерминированный, "
                     "НЕ загружает вторую модель). args: {args}  (например '--skip-gpu-check' "
                     "или '--skip-stack'). НЕ передавай --use-lmstudio/--model/--lmstudio.",
    "finish": "Завершить работу (ТОЛЬКО когда check_endpoints даёт _ГОТОВО_ПОЛНОСТЬЮ=ДА). "
              "args: {summary}",
}


# --------------------------------------------------------------------------- #
# Клиент LM Studio (устойчивый: text-режим + извлечение JSON + reasoning_content)
# --------------------------------------------------------------------------- #
class LMClient:
    PREFERRED_AGENT = ("qwen3-coder-30b-a3b-instruct", "devstral-small-2-24b-instruct-2512",
                       "qwen3-coder-next", "gpt-oss-120b", "gemma-4-31b-it-qat")
    MAX_TOKENS = 4096

    def __init__(self, base_url: str, model: str = "", timeout: int = 300) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or self._select_model()
        self.timeout = timeout
        log.info("ЕДИНАЯ модель для ВСЕЙ установки: %s "
                 "(в LM Studio должна быть загружена только она)", self.model)

    def _select_model(self) -> str:
        try:
            r = requests.get(f"{self.base_url}/models", timeout=10)
            ids = [m["id"] for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            return "local-model"
        for pref in self.PREFERRED_AGENT:
            if pref in ids:
                return pref
        chat = [i for i in ids if "embed" not in i.lower()]
        return chat[0] if chat else "local-model"

    def chat(self, messages: list[dict]) -> str:
        """Запрос к модели. Возвращает текст ответа (content или reasoning_content)."""
        for response_format in ({"type": "text"}, None):
            try:
                payload = {"model": self.model, "messages": messages,
                           "temperature": 0.1, "max_tokens": self.MAX_TOKENS}
                if response_format:
                    payload["response_format"] = response_format
                r = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout)
                if r.status_code in (400, 422):
                    continue
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                return (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
            except Exception as exc:  # noqa: BLE001
                log.warning("Ошибка запроса к LM Studio (%s) — пробую иначе.", exc)
                continue
        return ""


def extract_action(text: str) -> Optional[dict[str, Any]]:
    """Извлечь последний JSON-объект-действие из ответа модели."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    for cand in reversed(candidates):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and "action" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Автономный агент
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
Ты — автономный инженер-агент, который САМ разворачивает локальную мультиагентную
систему JARVIS-OS на Windows 11 (WSL2 + vLLM + Docker), GPU NVIDIA RTX 5090 (32 ГБ).

ЦЕЛЬ: полностью развернуть систему с нуля на диске {target_root}, чтобы заработали
сервисы: ядро http://localhost:8000, vLLM Qwen :8001, vLLM UI-TARS :8002, аудио :8003.
ВЕСЬ «тяжеляк» (веса моделей ~25 ГБ, образы Docker, образ WSL) — на диске {target_drive}.

ЕДИНАЯ МОДЕЛЬ: ВЕСЬ путь установки ведёшь ТЫ — одна и та же локальная модель.
НИКОГДА не запускай ничего, что загрузило бы вторую LLM в LM Studio (это вызывает
OOM на 32 ГБ GPU). В частности, run_bootstrap НЕ должен получать флаги
--use-lmstudio / --model / --lmstudio (он и так детерминированный).

ЦЕЛЕВАЯ АРХИТЕКТУРА (что должно работать В ИТОГЕ):
  На хосте Windows:
    - windows_rpc_bridge.py — демон, порт 8765 (мост управления хостом + HITL-гейт)
    - dashboard (Next.js 15) — порт 3000 (Command Center: деплой/десктоп/код/аудио)
  В Docker Desktop (WSL2):
    - backend (FastAPI ядро + LangGraph) — порт 8000
    - vLLM Qwen2.5-Coder-32B (Int4 AWQ) — порт 8001 (диспетчер + кодер, ~19 ГБ VRAM)
    - vLLM UI-TARS-7B (Int4) — порт 8002 (контроллер ОС/GUI, ~5.5 ГБ VRAM)
    - audio: Faster-Whisper Large-v3 + Kokoro TTS — порт 8003 (~2 ГБ VRAM)
    - sandbox — изолированное исполнение кода кодер-агента
  Весь «тяжеляк» (веса ~25 ГБ, образы Docker, образ WSL) — на диске {target_drive}.

КРИТЕРИЙ ГОТОВНОСТИ: check_endpoints возвращает _ГОТОВО_ПОЛНОСТЬЮ=ДА
(все 8000-8003 + dashboard 3000 + rpc_bridge 8765). Только тогда — finish.

ТЫ РАБОТАЕШЬ ЦИКЛАМИ. На каждом шаге верни СТРОГО ОДИН JSON-объект (без markdown,
без лишнего текста, без пошаговых рассуждений — сразу JSON):
  {{"thought": "кратко зачем", "action": "<имя>", "args": {{...}}}}

Доступные инструменты (action и args):
{tools}

ПЛАН (ориентир, действуй идемпотентно — сделанное не повторяй):
1. get_state — понять текущее состояние.
2. run_bootstrap — перенос на D:, .wslconfig, GPU, предзагрузка весов (докачка),
   подъём docker-стека (backend + vLLM x2 + audio + sandbox). Это основной рычаг.
3. Запустить RPC-мост: start_background command="python windows_rpc_bridge.py".
4. Дашборд: run_cmd "cd /d {target_root}\\dashboard && npm install" (если нет node —
   поставь через winget/choco), затем start_background command="npm run dev"
   cwd="{target_root}\\dashboard".
5. check_endpoints — дождаться _ГОТОВО_ПОЛНОСТЬЮ=ДА (сервисы прогреваются;
   при НЕТ — подожди/диагностируй логами docker compose и повтори проверку).
6. finish с итогом.

ПРИНЦИПЫ:
- При ошибке — диагностируй (читай вывод, docker compose logs <svc>) и исправляй сам.
- НЕ выполняй необратимых разрушительных команд (формат диска и т.п.) — заблокированы.
- finish ТОЛЬКО при _ГОТОВО_ПОЛНОСТЬЮ=ДА.

Отвечай ТОЛЬКО JSON-объектом действия.
"""


class InstallAgent:
    def __init__(self, lm: LMClient, tools: HostTools, *, max_steps: int = 80,
                 require_approval: bool = False, allow_catastrophic: bool = False) -> None:
        self.lm = lm
        self.tools = tools
        self.max_steps = max_steps
        self.require_approval = require_approval
        self.allow_catastrophic = allow_catastrophic
        self.history: list[dict] = []

    # -- гейты безопасности ---------------------------------------------- #
    def _command_of(self, action: str, args: dict[str, Any]) -> str:
        return str(args.get("command") or args.get("args") or args.get("path") or "")

    def _approve(self, action: str, command: str) -> bool:
        if not self.require_approval:
            return True
        try:
            ans = input(f"\n[ПОДТВЕРЖДЕНИЕ] {action}: {command}\n  Выполнить? [y/N]: ").strip().lower()
        except EOFError:
            return False
        return ans in ("y", "yes", "д", "да")

    def _gate(self, action: str, args: dict[str, Any]) -> Optional[str]:
        """Вернуть текст-блокировку, если действие нельзя выполнять; иначе None."""
        command = self._command_of(action, args)
        if command and is_catastrophic(command) and not self.allow_catastrophic:
            log.error("ЗАБЛОКИРОВАНО (катастрофа): %s", command)
            return ("ОТКАЗАНО: команда классифицирована как НЕОБРАТИМО разрушительная "
                    "(формат/очистка диска, удаление системы) и заблокирована политикой "
                    "безопасности. Выбери безопасную альтернативу.")
        if command and is_destructive(command) and not self._approve(action, command):
            return "ОТКАЗАНО оператором (деструктивная команда не подтверждена)."
        return None

    # -- основной цикл ---------------------------------------------------- #
    def run(self, goal: str) -> int:
        sys_prompt = SYSTEM_PROMPT.format(
            target_root=self.tools.target_root,
            target_drive=(self.tools.target_root.drive or "D:"),
            tools="\n".join(f"  - {k}: {v}" for k, v in TOOLS_DOC.items()),
        )
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": goal}]
        dispatch = {
            "get_state": self.tools.get_state,
            "run_powershell": self.tools.run_powershell,
            "run_cmd": self.tools.run_cmd,
            "run_wsl": self.tools.run_wsl,
            "docker": self.tools.docker,
            "read_file": self.tools.read_file,
            "write_file": self.tools.write_file,
            "http_get": self.tools.http_get,
            "start_background": self.tools.start_background,
            "check_endpoints": self.tools.check_endpoints,
            "run_bootstrap": self.tools.run_bootstrap,
        }
        last_signature = None
        repeat = 0

        for step in range(1, self.max_steps + 1):
            log.info("─" * 60)
            log.info("ШАГ %d/%d — запрашиваю решение модели…", step, self.max_steps)
            reply = self.lm.chat(messages)
            action_obj = extract_action(reply)

            if action_obj is None:
                log.warning("Модель не вернула валидное действие. Прошу повторить в формате JSON.")
                messages.append({"role": "assistant", "content": reply[:500]})
                messages.append({"role": "user", "content":
                                 "Верни СТРОГО один JSON-объект {thought, action, args}."})
                continue

            action = str(action_obj.get("action", ""))
            args = action_obj.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"command": args}
            thought = str(action_obj.get("thought", ""))[:300]
            log.info("МЫСЛЬ: %s", thought)
            log.info("ДЕЙСТВИЕ: %s  args=%s", action,
                     _truncate(json.dumps(args, ensure_ascii=False), 300))

            if action == "finish":
                log.info("✔ Агент завершил работу: %s", args.get("summary", ""))
                return 0

            if action not in dispatch:
                obs = f"Неизвестное действие '{action}'. Доступные: {', '.join(dispatch)} , finish."
                messages.append({"role": "assistant", "content": json.dumps(action_obj, ensure_ascii=False)})
                messages.append({"role": "user", "content": f"НАБЛЮДЕНИЕ:\n{obs}"})
                continue

            # Детектор зацикливания
            signature = json.dumps([action, args], ensure_ascii=False, sort_keys=True)
            repeat = repeat + 1 if signature == last_signature else 0
            last_signature = signature
            if repeat >= 3:
                obs = ("Ты повторяешь одно и то же действие без прогресса. Измени подход: "
                       "собери диагностику (логи docker compose, вывод команд) или заверши.")
                messages.append({"role": "assistant", "content": json.dumps(action_obj, ensure_ascii=False)})
                messages.append({"role": "user", "content": f"НАБЛЮДЕНИЕ:\n{obs}"})
                repeat = 0
                continue

            # Гейт безопасности
            blocked = self._gate(action, args)
            if blocked:
                messages.append({"role": "assistant", "content": json.dumps(action_obj, ensure_ascii=False)})
                messages.append({"role": "user", "content": f"НАБЛЮДЕНИЕ:\n{blocked}"})
                continue

            # Исполнение инструмента
            result = dispatch[action](**args) if isinstance(args, dict) else dispatch[action]()
            obs = f"код={result.get('returncode')}\n{result.get('output', '')}"
            log.info("НАБЛЮДЕНИЕ (код %s):\n%s", result.get("returncode"),
                     _truncate(result.get("output", ""), 1200))

            messages.append({"role": "assistant", "content": json.dumps(action_obj, ensure_ascii=False)})
            messages.append({"role": "user", "content": f"НАБЛЮДЕНИЕ:\n{_truncate(obs)}"})

        log.warning("Достигнут лимит шагов (%d). Останавливаюсь.", self.max_steps)
        return 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="JARVIS-OS — автономный агент-установщик на локальной LLM.")
    p.add_argument("--lmstudio", default="http://localhost:1234/v1")
    p.add_argument("--model", default="", help="ID модели LM Studio (по умолчанию — авто, coder/instruct).")
    p.add_argument("--target-root", default=r"D:\jarvis")
    p.add_argument("--max-steps", type=int, default=80)
    p.add_argument("--require-approval", action="store_true",
                   help="Спрашивать подтверждение перед деструктивными командами "
                        "(по умолчанию агент действует сам).")
    p.add_argument("--dry-run", action="store_true",
                   help="Не исполнять команды реально — только показывать намерения.")
    p.add_argument("--goal", default="", help="Доп. цель/уточнение для агента.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    repo_dir = Path(__file__).resolve().parent
    target_root = Path(args.target_root)

    # Файл-транскрипт
    try:
        log_dir = (target_root if target_root.drive and Path(target_root.drive + "\\").exists()
                   else repo_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"install_agent_{int(time.time())}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
        log.addHandler(fh)
        log.info("Транскрипт: %s", fh.baseFilename)
    except Exception:  # noqa: BLE001
        pass

    log.info("=" * 70)
    log.info("JARVIS-OS · ЕДИНАЯ автономная установка на ОДНОЙ локальной модели")
    log.info("Режим: %s | подтверждения: %s | max-шагов: %d",
             "DRY-RUN" if args.dry_run else "БОЕВОЙ",
             "да" if args.require_approval else "нет (полная автономия)", args.max_steps)
    log.info("Вся установка — одной моделью. bootstrap НЕ грузит вторую модель (защита от OOM).")
    log.info("Катастрофические команды (формат/очистка диска) — ВСЕГДА блокируются.")
    log.info("=" * 70)

    lm = LMClient(args.lmstudio, model=args.model)
    tools = HostTools(target_root, repo_dir, dry_run=args.dry_run)
    agent = InstallAgent(lm, tools, max_steps=args.max_steps,
                         require_approval=args.require_approval)

    goal = (f"Разверни JARVIS-OS ПОЛНОСТЬЮ и автономно на диске {target_root}. "
            f"Начни с get_state. Используй run_bootstrap для docker-стека, затем "
            f"подними RPC-мост (8765) и дашборд (3000) через start_background. "
            f"Цель достигнута, когда check_endpoints даёт _ГОТОВО_ПОЛНОСТЬЮ=ДА "
            f"(backend 8000, vLLM 8001/8002, audio 8003, dashboard 3000, rpc_bridge 8765). "
            f"Весь тяжеляк — на {target_root.drive or 'D:'}. {args.goal}").strip()

    try:
        return agent.run(goal)
    except KeyboardInterrupt:
        log.warning("Прервано пользователем (Ctrl+C).")
        return 130


if __name__ == "__main__":
    sys.exit(main())
