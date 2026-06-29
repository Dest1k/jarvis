# -*- coding: utf-8 -*-
"""
mcp_client.py — клиент Model Context Protocol (MCP) для JARVIS-OS.

Зачем: вместо хардкода КАЖДОГО инструмента агент подключается к локальным
**MCP-серверам** (стандарт Anthropic) и получает их инструменты динамически.
Серверы вендорятся ОФФЛАЙН в backend-образ (см. Dockerfile) — в рантайме сеть не
нужна.

Жизненный цикл (важно): stdio-сессии MCP держатся в anyio task-group, который
ПРИВЯЗАН к задаче. Поэтому вход и выход из контекстов делаем в ОДНОЙ длинной
задаче `_run` (вошли все сессии → ждём событие shutdown → вышли). Иначе anyio
кидает «cancel scope in a different task». Вызовы инструментов идут из других
задач — это безопасно (anyio-стримы рассчитаны на межзадачное общение).

Слой полностью НЕОБЯЗАТЕЛЕН и ИЗОЛИРОВАН: нет пакета `mcp` / сервер не поднялся —
агент работает как раньше.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Callable

log = logging.getLogger("jarvis.mcp")

MCP_CONFIG_PATH = os.environ.get("JARVIS_MCP_CONFIG", "/app/mcp_servers.json")
# Потолок числа MCP-инструментов, вливаемых в реестр (защита промпта диспетчера).
# При полном наборе включённых серверов их ~65 — держим запас (96), чтобы ничего
# не отбрасывалось молча. Если локальная модель «тонет» в инструментах — снижайте
# (или выключайте серверы в mcp_servers.json).
MCP_MAX_TOOLS = int(os.environ.get("JARVIS_MCP_MAX_TOOLS", "96"))
MCP_CALL_TIMEOUT = int(os.environ.get("JARVIS_MCP_CALL_TIMEOUT", "120"))

RegisterFn = Callable[[str, str, dict[str, Any], str], None]


class MCPManager:
    """Подключение к локальным MCP-серверам и диспетчеризация их инструментов."""

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self._tools: dict[str, tuple[str, str]] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()

    @staticmethod
    def _load_config() -> dict[str, Any]:
        try:
            with open(MCP_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
            return {k: v for k, v in servers.items() if not k.startswith("_")}
        except FileNotFoundError:
            log.info("MCP-конфиг %s не найден — MCP отключён.", MCP_CONFIG_PATH)
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP-конфиг не прочитан: %s", exc)
        return {}

    # --- запуск/останов ----------------------------------------------------
    async def start(self, register: RegisterFn) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(register))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=150)
        except asyncio.TimeoutError:
            log.warning("MCP: серверы поднимаются дольше 150с — продолжаю без ожидания.")

    async def _run(self, register: RegisterFn) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as exc:  # noqa: BLE001
            log.warning("Пакет 'mcp' недоступен — MCP-слой выключен (%s).", exc)
            self._ready.set()
            return
        try:
            async with AsyncExitStack() as stack:
                total = 0
                for name, spec in self._load_config().items():
                    if not spec.get("enabled", True):
                        self._info[name] = {"ok": False, "error": "disabled", "tools": []}
                        continue
                    try:
                        params = StdioServerParameters(
                            command=spec["command"],
                            args=list(spec.get("args", [])),
                            env={**os.environ, **(spec.get("env") or {})},
                        )
                        read, write = await stack.enter_async_context(stdio_client(params))
                        session = await stack.enter_async_context(ClientSession(read, write))
                        await asyncio.wait_for(session.initialize(), timeout=40)
                        listed = await asyncio.wait_for(session.list_tools(), timeout=30)
                        self._sessions[name] = session
                        names: list[str] = []
                        for t in listed.tools:
                            if total >= MCP_MAX_TOOLS:
                                break
                            qual = f"mcp_{name}_{t.name}"
                            self._tools[qual] = (name, t.name)
                            register(qual, (t.description or t.name),
                                     (t.inputSchema or {}), name)
                            names.append(t.name)
                            total += 1
                        self._info[name] = {"ok": True, "tools": names, "error": ""}
                        log.info("MCP '%s' подключён: инструментов %d.", name, len(names))
                    except Exception as exc:  # noqa: BLE001
                        self._info[name] = {"ok": False, "error": str(exc)[:200], "tools": []}
                        log.warning("MCP '%s' не поднялся: %s", name, exc)
                self._ready.set()
                await self._shutdown.wait()   # держим сессии открытыми в ЭТОЙ задаче
        except Exception:  # noqa: BLE001
            log.exception("MCP run-loop завершился с ошибкой")
            self._ready.set()

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except Exception:  # noqa: BLE001
                pass

    # --- вызов -------------------------------------------------------------
    async def call(self, qual_name: str, args: dict[str, Any]) -> dict[str, Any]:
        entry = self._tools.get(qual_name)
        if not entry:
            return {"ok": False, "content": f"MCP-инструмент '{qual_name}' не найден."}
        server, real = entry
        session = self._sessions.get(server)
        if session is None:
            return {"ok": False, "content": f"MCP-сервер '{server}' не подключён."}
        try:
            result = await asyncio.wait_for(
                session.call_tool(real, args or {}), timeout=MCP_CALL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "content": f"MCP '{qual_name}' ошибка: {exc}"}

        parts: list[str] = []
        for c in (getattr(result, "content", None) or []):
            if getattr(c, "type", "") == "text":
                parts.append(getattr(c, "text", ""))
            else:
                parts.append(f"[{getattr(c, 'type', 'content')}]")
        is_err = bool(getattr(result, "isError", False))
        return {"ok": not is_err, "content": "\n".join(parts).strip() or "(пусто)"}

    def status(self) -> dict[str, Any]:
        return {
            "servers": self._info,
            "tool_count": len(self._tools),
            "tools": sorted(self._tools.keys()),
        }


mcp_manager = MCPManager()
