# -*- coding: utf-8 -*-
"""mcp_client.py — resilient Model Context Protocol client for JARVIS-OS.

MCP is optional: if the package/server is unavailable, the core agent continues.
This version records per-server status, keeps sessions alive in a single long task,
and starts lightweight background runtime diagnostics after MCP boot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any, Callable

log = logging.getLogger("jarvis.mcp")

MCP_CONFIG_PATH = os.environ.get("JARVIS_MCP_CONFIG", "/app/mcp_servers.json")
MCP_MAX_TOOLS = int(os.environ.get("JARVIS_MCP_MAX_TOOLS", "96"))
MCP_CALL_TIMEOUT = int(os.environ.get("JARVIS_MCP_CALL_TIMEOUT", "120"))
MCP_START_TIMEOUT = int(os.environ.get("JARVIS_MCP_START_TIMEOUT", "150"))
MCP_RESTART_SEC = int(os.environ.get("JARVIS_MCP_RESTART_SEC", "20"))
RegisterFn = Callable[[str, str, dict[str, Any], str], None]

class MCPManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self._tools: dict[str, tuple[str, str]] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._runtime_task: asyncio.Task | None = None
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

    async def start(self, register: RegisterFn) -> None:
        if self._task is not None:
            return
        self._shutdown.clear()
        self._ready.clear()
        self._task = asyncio.create_task(self._supervised_run(register), name="mcp-supervisor")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=MCP_START_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("MCP: серверы поднимаются дольше %sс — продолжаю без ожидания.", MCP_START_TIMEOUT)
        await self._start_runtime_loop()

    async def _supervised_run(self, register: RegisterFn) -> None:
        while not self._shutdown.is_set():
            await self._run_once(register)
            if not self._shutdown.is_set():
                log.warning("MCP run-loop завершён; перезапуск через %sс.", MCP_RESTART_SEC)
                await asyncio.sleep(MCP_RESTART_SEC)

    def _resolve_command(self, command: str) -> str:
        # Absolute path when possible; otherwise leave module runners like python/npx.
        if os.path.isabs(command):
            return command
        found = shutil.which(command)
        return found or command

    async def _run_once(self, register: RegisterFn) -> None:
        self._sessions.clear(); self._tools.clear()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as exc:  # noqa: BLE001
            log.warning("Пакет 'mcp' недоступен — MCP-слой выключен (%s).", exc)
            self._ready.set(); return
        try:
            async with AsyncExitStack() as stack:
                total = 0
                for name, spec in self._load_config().items():
                    if not spec.get("enabled", True):
                        self._info[name] = {"ok": False, "error": "disabled", "tools": []}
                        continue
                    try:
                        params = StdioServerParameters(
                            command=self._resolve_command(str(spec["command"])),
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
                            register(qual, (t.description or t.name), (t.inputSchema or {}), name)
                            names.append(t.name); total += 1
                        self._info[name] = {"ok": True, "tools": names, "error": ""}
                        log.info("MCP '%s' подключён: инструментов %d.", name, len(names))
                    except Exception as exc:  # noqa: BLE001
                        self._info[name] = {"ok": False, "error": str(exc)[:300], "tools": []}
                        log.warning("MCP '%s' не поднялся: %s", name, exc)
                self._ready.set()
                await self._shutdown.wait()
        except Exception as exc:  # noqa: BLE001
            self._ready.set()
            log.exception("MCP run-loop завершился с ошибкой: %s", exc)

    async def _start_runtime_loop(self) -> None:
        if self._runtime_task is not None or os.environ.get("JARVIS_BACKGROUND_RUNTIME", "1") == "0":
            return
        async def _runtime() -> None:
            try:
                from .gpu_guard import GpuGuard
                from .idle_loop import BackgroundIdleLoop
                guard = GpuGuard(host_exec=None)
                await guard.start()
                loop = BackgroundIdleLoop(host_exec=None, broadcast=None, gpu_guard=guard)
                await loop.start()
                await self._shutdown.wait()
                await loop.stop(); await guard.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("Background runtime loop not started: %s", exc)
        self._runtime_task = asyncio.create_task(_runtime(), name="jarvis-background-runtime")

    async def stop(self) -> None:
        self._shutdown.set()
        for task in (self._runtime_task, self._task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=15)
            except Exception:  # noqa: BLE001
                task.cancel()
        self._runtime_task = None
        self._task = None

    async def call(self, qual_name: str, args: dict[str, Any]) -> dict[str, Any]:
        entry = self._tools.get(qual_name)
        if not entry:
            return {"ok": False, "content": f"MCP-инструмент '{qual_name}' не найден."}
        server, real = entry
        session = self._sessions.get(server)
        if session is None:
            return {"ok": False, "content": f"MCP-сервер '{server}' не подключён."}
        try:
            result = await asyncio.wait_for(session.call_tool(real, args or {}), timeout=MCP_CALL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self._info.setdefault(server, {})["error"] = str(exc)[:300]
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
        return {"servers": self._info, "tool_count": len(self._tools), "tools": sorted(self._tools.keys())}

mcp_manager = MCPManager()
