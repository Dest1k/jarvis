# -*- coding: utf-8 -*-
"""mcp_client.py — resilient Model Context Protocol client for JARVIS-OS.

MCP is optional: if the package/server is unavailable, the core agent continues.
This supervisor validates server commands/paths, records per-server status, and
retries failed servers instead of leaving Git/SQLite offline until backend restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("jarvis.mcp")

MCP_CONFIG_PATH = os.environ.get("JARVIS_MCP_CONFIG", "/app/mcp_servers.json")
MCP_MAX_TOOLS = int(os.environ.get("JARVIS_MCP_MAX_TOOLS", "96"))
MCP_CALL_TIMEOUT = int(os.environ.get("JARVIS_MCP_CALL_TIMEOUT", "120"))
MCP_START_TIMEOUT = int(os.environ.get("JARVIS_MCP_START_TIMEOUT", "150"))
MCP_RESTART_SEC = int(os.environ.get("JARVIS_MCP_RESTART_SEC", "20"))
RegisterFn = Callable[[str, str, dict[str, Any], str], None]


def _is_module_runner(command: str, args: list[Any]) -> bool:
    return command in ("python", "python3", "py") and len(args) >= 2 and args[0] == "-m"


class MCPManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self._tools: dict[str, tuple[str, str]] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._restart_count = 0

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

    async def _supervised_run(self, register: RegisterFn) -> None:
        while not self._shutdown.is_set():
            self._restart_count += 1
            failed = await self._run_once(register)
            if self._shutdown.is_set():
                break
            delay = MCP_RESTART_SEC if failed else max(MCP_RESTART_SEC * 6, 120)
            log.warning("MCP supervisor cycle завершён; failed=%d; следующий цикл через %sс.", failed, delay)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    def _resolve_command(self, command: str) -> str:
        if os.path.isabs(command):
            return command
        found = shutil.which(command)
        return found or command

    def _validate_spec(self, name: str, spec: dict[str, Any]) -> tuple[str, list[str]]:
        warnings: list[str] = []
        command = str(spec.get("command", "")).strip()
        args = list(spec.get("args", []))
        resolved = self._resolve_command(command)
        if not command:
            warnings.append("empty command")
        elif os.path.isabs(command) and not Path(command).exists():
            warnings.append(f"absolute command path does not exist: {command}")
        elif not os.path.isabs(resolved) and not _is_module_runner(command, args) and command not in ("npx", "npm"):
            warnings.append(f"command not found in PATH: {command}")

        # Known server-specific path checks. These are warnings, not hard errors:
        # some paths are created after first boot.
        if name == "sqlite" and "--db-path" in args:
            i = args.index("--db-path")
            if i + 1 < len(args):
                p = Path(str(args[i + 1]))
                if not p.is_absolute():
                    warnings.append(f"sqlite db path should be absolute: {p}")
                elif not p.parent.exists():
                    warnings.append(f"sqlite db parent missing: {p.parent}")
        if name == "git" and "--repository" in args:
            i = args.index("--repository")
            if i + 1 < len(args):
                p = Path(str(args[i + 1]))
                if not p.is_absolute():
                    warnings.append(f"git repository path should be absolute: {p}")
                elif not (p / ".git").exists():
                    warnings.append(f"git worktree marker missing: {p}/.git")
        return resolved, warnings

    async def _run_once(self, register: RegisterFn) -> int:
        self._sessions.clear()
        self._tools.clear()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as exc:  # noqa: BLE001
            log.warning("Пакет 'mcp' недоступен — MCP-слой выключен (%s).", exc)
            self._info["__package__"] = {"ok": False, "error": str(exc), "tools": []}
            self._ready.set()
            return 1

        failed = 0
        try:
            async with AsyncExitStack() as stack:
                total = 0
                config = self._load_config()
                if not config:
                    self._ready.set()
                    await self._shutdown.wait()
                    return 0

                for name, spec in config.items():
                    if not spec.get("enabled", True):
                        self._info[name] = {"ok": False, "error": "disabled", "tools": [], "warnings": []}
                        continue
                    started_at = time.time()
                    try:
                        command, warnings = self._validate_spec(name, spec)
                        params = StdioServerParameters(
                            command=command,
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
                            if qual not in self._tools:
                                self._tools[qual] = (name, t.name)
                                register(qual, (t.description or t.name), (t.inputSchema or {}), name)
                            names.append(t.name)
                            total += 1
                        self._info[name] = {
                            "ok": True, "tools": names, "error": "", "warnings": warnings,
                            "command": command, "args": list(spec.get("args", [])),
                            "started_at": started_at, "last_ok_at": time.time(),
                        }
                        log.info("MCP '%s' подключён: инструментов %d.", name, len(names))
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        resolved, warnings = self._validate_spec(name, spec)
                        self._info[name] = {
                            "ok": False, "error": str(exc)[:500], "tools": [], "warnings": warnings,
                            "command": resolved, "args": list(spec.get("args", [])),
                            "started_at": started_at, "last_failed_at": time.time(),
                        }
                        log.warning("MCP '%s' не поднялся: %s", name, exc)
                self._ready.set()
                if failed:
                    # Retry failed servers periodically. This intentionally restarts the
                    # whole stdio stack; MCP is optional and tools re-register idempotently.
                    try:
                        await asyncio.wait_for(self._shutdown.wait(), timeout=MCP_RESTART_SEC)
                    except asyncio.TimeoutError:
                        return failed
                else:
                    await self._shutdown.wait()
                return failed
        except Exception as exc:  # noqa: BLE001
            self._ready.set()
            log.exception("MCP run-loop завершился с ошибкой: %s", exc)
            return max(1, failed)

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except Exception:  # noqa: BLE001
                self._task.cancel()
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
            self._info.setdefault(server, {})["last_ok_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            self._info.setdefault(server, {})["error"] = str(exc)[:500]
            self._info.setdefault(server, {})["last_failed_at"] = time.time()
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
            "restart_count": self._restart_count,
            "config_path": MCP_CONFIG_PATH,
        }


mcp_manager = MCPManager()
