#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
windows_rpc_bridge.py — защищённый асинхронный RPC-демон на Windows-хосте.

Зачем нужен:
    Центральный оркестратор (LangGraph) работает внутри Linux/WSL2 и не может
    напрямую управлять хостом Windows. Этот демон предоставляет ему безопасный
    канал управления хостом (операции ОС, медиа-хуки, запуск приложений)
    через локальные защищённые WebSockets с токен-хендшейком.

Ключевые свойства безопасности:
    • Слушает ИСКЛЮЧИТЕЛЬНО 127.0.0.1 (никаких внешних подключений).
    • Токен-хендшейк: первое сообщение клиента обязано содержать одноразовый
      токен из ~/.jarvis/bridge.token (генерируется при старте, права владельца).
    • HITL-гейт (Human-in-the-Loop): любая команда из чёрного списка
      (удаление, форматирование, git push, сетевые операции, запуск процессов)
      ОСТАНАВЛИВАЕТ исполнение и ждёт визуального подтверждения в дашборде.
    • Git-автоматика: целевая ветка КОНФИГУРИРУЕМА (JARVIS_GIT_BRANCH,
      по умолчанию 'jarvis/auto-updates'). Пуш в 'main' запрещён без явного
      флага оператора — чтобы ИИ-сгенерированный код не попадал в основную
      ветку без ревью.

Запуск (PowerShell на хосте Windows):
    python windows_rpc_bridge.py --port 8765

Зависимости: pip install websockets
Опционально (для нативных хуков ОС): pyautogui, psutil, pywin32.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shlex
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:  # pragma: no cover
    print("[ОШИБКА] Требуется пакет 'websockets'. Установите: pip install websockets")
    sys.exit(1)


def _setup_console_utf8() -> None:
    """UTF-8 для консоли Windows (иначе кириллица — «ромбики»). До basicConfig."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


_setup_console_utf8()

# --------------------------------------------------------------------------- #
# Журналирование
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | RPC | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis.rpc")


# --------------------------------------------------------------------------- #
# Конфигурация и токен
# --------------------------------------------------------------------------- #
JARVIS_HOME = Path.home() / ".jarvis"
TOKEN_PATH = JARVIS_HOME / "bridge.token"

# Ветка git по умолчанию — НЕ main. ИИ-генерируемый код не должен попадать
# в основную ветку без ревью оператора.
DEFAULT_GIT_BRANCH = os.environ.get("JARVIS_GIT_BRANCH", "jarvis/auto-updates")
ALLOW_PUSH_TO_MAIN = os.environ.get("JARVIS_ALLOW_PUSH_MAIN", "0") == "1"

# Время ожидания решения оператора в HITL-гейте (сек)
HITL_TIMEOUT_SEC = int(os.environ.get("JARVIS_HITL_TIMEOUT", "180"))


def ensure_token() -> str:
    """Сгенерировать (или прочитать) одноразовый токен авторизации."""
    JARVIS_HOME.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            log.info("Использую существующий токен: %s", TOKEN_PATH)
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)  # на Windows частично игнорируется, но не вредит
    except OSError:
        pass
    log.info("Сгенерирован новый токен RPC-моста: %s", TOKEN_PATH)
    return token


# --------------------------------------------------------------------------- #
# Классификация команд — определение «деструктивности» для HITL-гейта
# --------------------------------------------------------------------------- #
# Однословные опасные команды/подкоманды. Сверяются с ТОКЕНАМИ команды, а не
# подстрокой; токены-флаги (начинаются с '-') игнорируются — иначе безобидный
# '--format' ложно ловился на 'format' (и каждый опрос статуса из дашборда
# требовал подтверждения).
DESTRUCTIVE_TOKENS = {
    "rm", "rmdir", "del", "erase", "format", "mkfs", "diskpart",
    "shutdown", "reboot", "restart-computer", "stop-computer", "stop-process",
    "taskkill", "kill", "remove-item", "rd",
    "curl", "wget", "invoke-webrequest", "iwr",  # сетевые операции (эксфильтрация)
}
# Многословные опасные паттерны — сверяются как подстрока.
DESTRUCTIVE_PHRASES = (
    "git push", "git reset --hard", "git clean", "git checkout -- ",
    "reg delete", "net stop", "sc delete", "schtasks /delete",
    "pip uninstall", "npm uninstall",
)
# Явно READ-ONLY команды (статусные опросы дашборда) — НИКОГДА не требуют HITL.
# Сверяются по началу нормализованной команды.
READONLY_PREFIXES = (
    "docker ps", "docker logs", "docker inspect", "docker version",
    "docker stats", "docker compose ps", "docker compose ls",
    "nvidia-smi", "cmd /c dir", "dir ", "type ", "where ", "ver",
    "wmic", "systeminfo", "tasklist", "get-content", "gc ",
)


def is_destructive(action: str, payload: dict[str, Any]) -> bool:
    """Определить, требует ли операция HITL-подтверждения."""
    # Явный флаг от вызывающей стороны
    if payload.get("force_confirm"):
        return True
    if action in ("git_push", "delete_path", "kill_process", "system_power"):
        return True
    if action in ("exec", "powershell", "open_app"):
        cmd = str(payload.get("command", "")).strip().lower()
        # 1) read-only статусные команды — без HITL (иначе опрос дашборда раз в
        #    5 с заваливает оператора подтверждениями).
        if any(cmd.startswith(p) for p in READONLY_PREFIXES):
            return False
        # 2) многословные опасные фразы — подстрокой.
        if any(ph in cmd for ph in DESTRUCTIVE_PHRASES):
            return True
        # 3) однословные маркеры — по токенам, игнорируя флаги (-...).
        tokens = [t for t in re.split(r"[\s|;&()]+", cmd) if t and not t.startswith("-")]
        return any(t in DESTRUCTIVE_TOKENS for t in tokens)
    return False


# --------------------------------------------------------------------------- #
# HITL-гейт: реестр ожидающих подтверждений
# --------------------------------------------------------------------------- #
@dataclass
class PendingApproval:
    """Ожидающий подтверждения запрос."""
    approval_id: str
    action: str
    summary: str
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future = field(default_factory=asyncio.Future)


class ApprovalRegistry:
    """Хранилище ожидающих HITL-подтверждений + broadcast подписчикам (дашборд)."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._subscribers: set[WebSocketServerProtocol] = set()

    def subscribe(self, ws: WebSocketServerProtocol) -> None:
        self._subscribers.add(ws)

    def unsubscribe(self, ws: WebSocketServerProtocol) -> None:
        self._subscribers.discard(ws)

    async def request_approval(self, action: str, summary: str,
                               payload: dict[str, Any]) -> PendingApproval:
        """Зарегистрировать запрос и оповестить дашборд."""
        approval = PendingApproval(
            approval_id=str(uuid.uuid4()),
            action=action,
            summary=summary,
            payload=payload,
        )
        self._pending[approval.approval_id] = approval
        await self._broadcast({
            "type": "hitl_request",
            "approval_id": approval.approval_id,
            "action": action,
            "summary": summary,
            "created_at": approval.created_at,
        })
        log.warning("HITL: ожидаю подтверждения [%s] %s", approval.approval_id[:8], summary)
        return approval

    def resolve(self, approval_id: str, approved: bool, operator: str = "dashboard") -> bool:
        """Разрешить ожидающий запрос (вызывается из обработчика решения оператора)."""
        approval = self._pending.pop(approval_id, None)
        if not approval or approval.future.done():
            return False
        approval.future.set_result({"approved": approved, "operator": operator})
        log.info("HITL: запрос [%s] %s оператором %s",
                 approval_id[:8], "ОДОБРЕН" if approved else "ОТКЛОНЁН", operator)
        return True

    async def _broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        for ws in self._subscribers:
            try:
                await ws.send(json.dumps(message, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self._subscribers.discard(ws)


# --------------------------------------------------------------------------- #
# Утилита: размеры PNG из заголовка (IHDR), без внешних зависимостей
# --------------------------------------------------------------------------- #
def _png_size(data: bytes) -> tuple[int, int]:
    """Вернуть (width, height) PNG по сигнатуре+IHDR или (0, 0)."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return 0, 0


# --------------------------------------------------------------------------- #
# Исполнители нативных хуков ОС Windows
# --------------------------------------------------------------------------- #
class HostExecutor:
    """Набор нативных операций на хосте Windows."""

    @staticmethod
    async def _run(cmd: list[str] | str, shell: bool = False,
                   timeout: int = 120) -> dict[str, Any]:
        """Асинхронно выполнить процесс и вернуть структурированный результат."""
        loop = asyncio.get_running_loop()

        def _blocking() -> dict[str, Any]:
            proc = subprocess.run(
                cmd, shell=shell, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

        return await loop.run_in_executor(None, _blocking)

    async def exec_command(self, command: str) -> dict[str, Any]:
        """Выполнить произвольную команду cmd.exe."""
        log.info("Выполняю команду хоста: %s", command)
        return await self._run(command, shell=True)

    async def read_file(self, path: str) -> dict[str, Any]:
        """Прочитать файл на хосте (для редактора конфигурации в дашборде)."""
        loop = asyncio.get_running_loop()

        def _b() -> dict[str, Any]:
            try:
                return {"returncode": 0,
                        "stdout": Path(path).read_text(encoding="utf-8", errors="replace")}
            except Exception as exc:  # noqa: BLE001
                return {"returncode": 1, "stderr": str(exc)}

        return await loop.run_in_executor(None, _b)

    async def write_file(self, path: str, content: str) -> dict[str, Any]:
        """Записать файл на хосте (сохранение конфигурации из дашборда)."""
        loop = asyncio.get_running_loop()

        def _b() -> dict[str, Any]:
            try:
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                return {"returncode": 0, "stdout": f"Записано {len(content)} символов в {path}"}
            except Exception as exc:  # noqa: BLE001
                return {"returncode": 1, "stderr": str(exc)}

        return await loop.run_in_executor(None, _b)

    async def powershell(self, command: str) -> dict[str, Any]:
        """Выполнить PowerShell-команду."""
        log.info("PowerShell: %s", command)
        return await self._run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        )

    async def open_app(self, command: str) -> dict[str, Any]:
        """Запустить приложение/исполняемый файл на хосте."""
        log.info("Запуск приложения: %s", command)
        return await self._run(f'start "" {command}', shell=True)

    async def media_hook(self, key: str) -> dict[str, Any]:
        """Управление медиа (play/pause/next/prev/volume) через виртуальные клавиши."""
        # Соответствие медиа-клавиш виртуальным кодам Windows (VK)
        vk_map = {
            "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1,
            "stop": 0xB2, "vol_up": 0xAF, "vol_down": 0xAE, "mute": 0xAD,
        }
        vk = vk_map.get(key)
        if vk is None:
            return {"returncode": 1, "stderr": f"Неизвестная медиа-клавиша: {key}"}
        ps = (
            f"$sig='[DllImport(\"user32.dll\")]public static extern void keybd_event"
            f"(byte b,byte s,uint f,int e);';"
            f"$t=Add-Type -MemberDefinition $sig -Name K -Namespace W -PassThru;"
            f"$t::keybd_event({vk},0,1,0);$t::keybd_event({vk},0,3,0)"
        )
        return await self.powershell(ps)

    async def screenshot(self, out_path: Optional[str] = None,
                         return_b64: bool = False) -> dict[str, Any]:
        """
        Сделать скриншот экрана хоста (для контекста UI-TARS).

        При return_b64=True дополнительно возвращает PNG в base64 и размеры
        экрана — это нужно агенту в WSL/контейнере, который НЕ имеет доступа к
        файловой системе хоста и не может прочитать сохранённый файл.
        """
        runtime_dir = JARVIS_HOME / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        out = out_path or str(runtime_dir / f"shot_{int(time.time())}.png")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
            f"$bmp.Save('{out}');"
        )
        res = await self.powershell(ps)
        res["path"] = out
        if return_b64:
            try:
                data = Path(out).read_bytes()
                res["image_b64"] = base64.b64encode(data).decode("ascii")
                w, h = _png_size(data)
                if w and h:
                    res["screen_w"], res["screen_h"] = w, h
            except Exception as exc:  # noqa: BLE001
                res["screenshot_error"] = str(exc)
        return res

    # --- Нативный ввод (мышь/клавиатура) для UI-TARS-управления хостом ----- #
    @staticmethod
    def _pyautogui():
        try:
            import pyautogui
            pyautogui.FAILSAFE = False
            return pyautogui
        except Exception:  # noqa: BLE001
            return None

    async def _input_exec(self, fn) -> dict[str, Any]:
        """Выполнить синхронный pyautogui-вызов в пуле потоков."""
        pg = self._pyautogui()
        if pg is None:
            return {"returncode": 1,
                    "stderr": "pyautogui не установлен (pip install pyautogui)."}
        loop = asyncio.get_running_loop()

        def _b() -> dict[str, Any]:
            try:
                fn(pg)
                return {"returncode": 0, "stdout": "ok"}
            except Exception as exc:  # noqa: BLE001
                return {"returncode": 1, "stderr": str(exc)}

        return await loop.run_in_executor(None, _b)

    async def mouse_move(self, x: int, y: int) -> dict[str, Any]:
        return await self._input_exec(lambda pg: pg.moveTo(int(x), int(y)))

    async def mouse_click(self, x: Optional[int] = None, y: Optional[int] = None,
                          button: str = "left", double: bool = False) -> dict[str, Any]:
        def _do(pg):
            kw = {"button": button}
            if x is not None and y is not None:
                kw.update(x=int(x), y=int(y))
            (pg.doubleClick if double else pg.click)(**kw)
        return await self._input_exec(_do)

    async def type_text(self, text: str) -> dict[str, Any]:
        return await self._input_exec(lambda pg: pg.typewrite(str(text), interval=0.01))

    async def key_press(self, keys: str) -> dict[str, Any]:
        # 'ctrl+s' → hotkey('ctrl','s'); 'enter' → press('enter')
        parts = [k.strip() for k in str(keys).replace("+", " ").split() if k.strip()]
        if not parts:
            return {"returncode": 1, "stderr": "Не заданы клавиши."}
        if len(parts) == 1:
            return await self._input_exec(lambda pg: pg.press(parts[0]))
        return await self._input_exec(lambda pg: pg.hotkey(*parts))

    async def scroll(self, amount: int = -3) -> dict[str, Any]:
        return await self._input_exec(lambda pg: pg.scroll(int(amount) * 120))

    async def kill_process(self, name: str) -> dict[str, Any]:
        """Завершить процесс по имени (деструктивно → проходит через HITL)."""
        return await self._run(f'taskkill /IM "{name}" /F', shell=True)

    async def system_power(self, mode: str) -> dict[str, Any]:
        """Управление питанием (деструктивно → HITL)."""
        mapping = {
            "shutdown": "shutdown /s /t 30",
            "reboot": "shutdown /r /t 30",
            "cancel": "shutdown /a",
            "lock": "rundll32.exe user32.dll,LockWorkStation",
        }
        cmd = mapping.get(mode)
        if not cmd:
            return {"returncode": 1, "stderr": f"Неизвестный режим питания: {mode}"}
        return await self._run(cmd, shell=True)


# --------------------------------------------------------------------------- #
# Git-автоматика (конфигурируемая ветка; пуш в main — только по явному флагу)
# --------------------------------------------------------------------------- #
class GitAutomation:
    """Безопасная git-автоматика для динамических обновлений кода агентом."""

    def __init__(self, executor: HostExecutor) -> None:
        self.exec = executor

    async def commit_and_push(self, repo: str, message: str,
                              branch: Optional[str] = None) -> dict[str, Any]:
        """
        Закоммитить и запушить изменения в КОНФИГУРИРУЕМУЮ ветку.

        ПРЕДОХРАНИТЕЛЬ: пуш в 'main' разрешён только при JARVIS_ALLOW_PUSH_MAIN=1.
        Это осознанная мера: автоматический пуш ИИ-сгенерированного кода прямо
        в основную ветку без ревью — источник трудноуловимых регрессий.
        """
        target = branch or DEFAULT_GIT_BRANCH
        if target == "main" and not ALLOW_PUSH_TO_MAIN:
            return {
                "returncode": 1,
                "stderr": (
                    "Отказано: прямой пуш в 'main' запрещён политикой безопасности. "
                    "Используйте feature-ветку или установите JARVIS_ALLOW_PUSH_MAIN=1 "
                    "осознанно (под ответственность оператора)."
                ),
            }

        repo_q = repo
        steps = [
            f'git -C "{repo_q}" rev-parse --is-inside-work-tree',
            f'git -C "{repo_q}" checkout -B "{target}"',
            f'git -C "{repo_q}" add -A',
            f'git -C "{repo_q}" commit -m {shlex.quote(message)} || echo "нет изменений"',
            f'git -C "{repo_q}" push -u origin "{target}"',
        ]
        results = []
        for step in steps:
            r = await self.exec.exec_command(step)
            results.append({"cmd": step, **r})
            if r["returncode"] != 0 and "нет изменений" not in (r.get("stdout") or ""):
                # Останавливаемся на первой настоящей ошибке
                if "commit" not in step:
                    break
        return {"returncode": 0, "branch": target, "steps": results}


# --------------------------------------------------------------------------- #
# Маршрутизатор RPC-вызовов
# --------------------------------------------------------------------------- #
class RpcRouter:
    """Сопоставляет action → обработчик, применяет HITL-гейт."""

    def __init__(self, registry: ApprovalRegistry) -> None:
        self.registry = registry
        self.host = HostExecutor()
        self.git = GitAutomation(self.host)
        self.handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "exec": lambda p: self.host.exec_command(p["command"]),
            "powershell": lambda p: self.host.powershell(p["command"]),
            "open_app": lambda p: self.host.open_app(p["command"]),
            "media_hook": lambda p: self.host.media_hook(p["key"]),
            "screenshot": lambda p: self.host.screenshot(
                p.get("path"), p.get("return_b64", False)),
            "mouse_move": lambda p: self.host.mouse_move(p["x"], p["y"]),
            "mouse_click": lambda p: self.host.mouse_click(
                p.get("x"), p.get("y"), p.get("button", "left"), p.get("double", False)),
            "type_text": lambda p: self.host.type_text(p.get("text", "")),
            "key_press": lambda p: self.host.key_press(p.get("keys", "")),
            "scroll": lambda p: self.host.scroll(p.get("amount", -3)),
            "kill_process": lambda p: self.host.kill_process(p["name"]),
            "system_power": lambda p: self.host.system_power(p["mode"]),
            "read_file": lambda p: self.host.read_file(p["path"]),
            "write_file": lambda p: self.host.write_file(p["path"], p.get("content", "")),
            "git_push": lambda p: self.git.commit_and_push(
                p["repo"], p.get("message", "JARVIS: авто-обновление"), p.get("branch")
            ),
        }

    async def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Выполнить action, при необходимости пройдя HITL-гейт."""
        handler = self.handlers.get(action)
        if handler is None:
            return {"ok": False, "error": f"Неизвестное действие: {action}"}

        # --- HITL-гейт для деструктивных операций ---
        if is_destructive(action, payload):
            summary = self._summarize(action, payload)
            approval = await self.registry.request_approval(action, summary, payload)
            try:
                decision = await asyncio.wait_for(approval.future, timeout=HITL_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                self.registry.resolve(approval.approval_id, approved=False, operator="timeout")
                return {"ok": False, "error": "HITL: истекло время ожидания подтверждения.",
                        "halted": True}
            if not decision.get("approved"):
                return {"ok": False, "error": "HITL: операция отклонена оператором.",
                        "halted": True}
            log.info("HITL: операция одобрена, продолжаю исполнение [%s].", action)

        # --- Исполнение ---
        try:
            result = await handler(payload)
            return {"ok": result.get("returncode", 0) == 0, "result": result}
        except KeyError as exc:
            return {"ok": False, "error": f"Отсутствует обязательный параметр: {exc}"}
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка исполнения action=%s", action)
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _summarize(action: str, payload: dict[str, Any]) -> str:
        """Краткое человекочитаемое описание для дашборда."""
        if action == "git_push":
            return f"git push → ветка '{payload.get('branch', DEFAULT_GIT_BRANCH)}' в {payload.get('repo')}"
        if action in ("exec", "powershell", "open_app"):
            return f"{action}: {payload.get('command')}"
        if action == "kill_process":
            return f"Завершить процесс: {payload.get('name')}"
        if action == "system_power":
            return f"Питание системы: {payload.get('mode')}"
        return f"{action}: {json.dumps(payload, ensure_ascii=False)[:120]}"


# --------------------------------------------------------------------------- #
# WebSocket-сервер
# --------------------------------------------------------------------------- #
class RpcBridgeServer:
    """Главный WebSocket-сервер RPC-моста."""

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.token = token
        self.host = host
        self.port = port
        self.registry = ApprovalRegistry()
        self.router = RpcRouter(self.registry)

    async def _authenticate(self, ws: WebSocketServerProtocol) -> bool:
        """Токен-хендшейк: первое сообщение клиента — авторизация."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
        except (asyncio.TimeoutError, json.JSONDecodeError):
            await ws.send(json.dumps({"type": "auth", "ok": False,
                                      "error": "Ожидалось JSON-сообщение авторизации."}))
            return False
        if msg.get("type") != "auth" or not secrets.compare_digest(
            str(msg.get("token", "")), self.token
        ):
            await ws.send(json.dumps({"type": "auth", "ok": False,
                                      "error": "Неверный токен."}))
            log.warning("Отклонено подключение с неверным токеном от %s", ws.remote_address)
            return False
        role = msg.get("role", "orchestrator")
        await ws.send(json.dumps({"type": "auth", "ok": True, "role": role}))
        log.info("Авторизовано подключение (role=%s) от %s", role, ws.remote_address)
        # HITL-уведомления получают ВСЕ авторизованные подключения. Важно:
        # браузерный дашборд не ходит в мост напрямую — он подключён к ядру
        # (backend), а ядро держит ЕДИНСТВЕННОЕ соединение с мостом с
        # role=orchestrator и РЕТРАНСЛИРУЕТ hitl_request в дашборд (/ws/hitl).
        # Поэтому подписываем и оркестратор, иначе HITL-модал не всплывёт и
        # деструктивная команда «зависнет» в ожидании решения.
        self.registry.subscribe(ws)
        return True

    async def handler(self, ws: WebSocketServerProtocol) -> None:
        """Обработчик одного клиентского соединения."""
        if not await self._authenticate(ws):
            await ws.close()
            return
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({"type": "error", "error": "Некорректный JSON."}))
                    continue

                msg_type = msg.get("type")

                # Решение оператора по HITL-запросу (из дашборда)
                if msg_type == "hitl_decision":
                    ok = self.registry.resolve(
                        msg.get("approval_id", ""),
                        approved=bool(msg.get("approved")),
                        operator=msg.get("operator", "dashboard"),
                    )
                    await ws.send(json.dumps({"type": "hitl_ack", "ok": ok}))
                    continue

                # RPC-вызов от оркестратора
                if msg_type == "rpc":
                    req_id = msg.get("id", str(uuid.uuid4()))
                    action = msg.get("action", "")
                    payload = msg.get("payload", {}) or {}
                    result = await self.router.dispatch(action, payload)
                    await ws.send(json.dumps(
                        {"type": "rpc_result", "id": req_id, **result},
                        ensure_ascii=False,
                    ))
                    continue

                if msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                    continue

                await ws.send(json.dumps({"type": "error",
                                          "error": f"Неизвестный тип сообщения: {msg_type}"}))
        except websockets.ConnectionClosed:
            log.info("Соединение закрыто: %s", ws.remote_address)
        finally:
            self.registry.unsubscribe(ws)

    async def serve(self) -> None:
        log.info("=" * 60)
        log.info("JARVIS-OS · RPC-мост хоста запущен")
        log.info("Адрес:        ws://%s:%s (только localhost)", self.host, self.port)
        log.info("Токен:        %s", TOKEN_PATH)
        log.info("Git-ветка:    %s (пуш в main: %s)",
                 DEFAULT_GIT_BRANCH, "разрешён" if ALLOW_PUSH_TO_MAIN else "ЗАПРЕЩЁН")
        log.info("HITL-таймаут: %s сек", HITL_TIMEOUT_SEC)
        log.info("=" * 60)
        async with websockets.serve(
            self.handler, self.host, self.port,
            max_size=16 * 1024 * 1024,   # до 16 МБ (скриншоты/кадры)
            ping_interval=20, ping_timeout=20,
        ):
            await asyncio.Future()  # работать бесконечно


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="JARVIS-OS — защищённый RPC-мост хоста Windows.")
    p.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания (только localhost).")
    p.add_argument("--port", type=int, default=8765, help="Порт WebSocket.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.error("Из соображений безопасности RPC-мост слушает только localhost. "
                  "Запрошенный host=%s отклонён.", args.host)
        return 2
    # Рабочий каталог — корень проекта: относительные пути из дашборда
    # (wsl/.env, data/models, hf_downloader.py, compose) резолвятся корректно.
    os.chdir(Path(__file__).resolve().parent)
    token = ensure_token()
    server = RpcBridgeServer(token=token, host=args.host, port=args.port)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        log.info("Остановка RPC-моста по Ctrl+C.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
