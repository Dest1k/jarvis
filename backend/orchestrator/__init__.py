# -*- coding: utf-8 -*-
"""
Пакет оркестрации JARVIS-OS — агентская прослойка между запросом пользователя,
локальной Gemma-диспетчер моделью, инструментами, когнитивной БД и хостом.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, AsyncIterator, Optional

from . import agent as agent  # noqa: F401
from . import persona

log = logging.getLogger("jarvis.orchestrator")

# agent.py читает флаг автономности при импорте. В тестах и smoke-прогонах пакет
# иногда уже импортирован в процессе до установки env, поэтому синхронизируем флаг
# здесь и перед каждым ходом (см. run_chat ниже). Это не выключает автономность в
# обычном запуске, а делает JARVIS_AUTONOMOUS=0 надёжным и детерминированным.
def _sync_runtime_flags() -> None:
    try:
        agent.AUTONOMOUS_ENABLED = os.environ.get("JARVIS_AUTONOMOUS", "1") != "0"  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.debug("runtime flag sync skipped: %s", exc)


_sync_runtime_flags()

_NATIVE_MANDATE = (
    "NATIVE LOW-LEVEL CONTROL MANDATE: работай с ОС и приложениями на системном "
    "уровне. Для системной информации, процессов, служб, событий, железа, окон и "
    "элементов интерфейса сначала используй native_host/native_window/native_ui "
    "(WMI/CIM, Win32 HWND, UI Automation, доступные Linux desktop APIs). "
    "windows.exec/powershell и текстовый CLI-парсинг — fallback, только если "
    "native-инструмент недоступен или вернул недостаточно данных. Визуальные клики "
    "через gui — последний fallback, когда native/API/CLI путь не сработал или "
    "цель действительно только интерактивная. Если пользователь просит открыть "
    "консоль и видеть вывод в ней — открывай реальное окно терминала; если просит "
    "просто получить данные — выполняй команду и кратко пересказывай наблюдение. "
    "Кластеризацию НЕ включай и не предлагай: это дорожная карта, текущий режим — "
    "локальный JARVIS без offload на внешние узлы. Для широких целей используй "
    "mission:plan/status/execute/run_role/learning_tick, чтобы создавать durable "
    "project plan, исполнять runnable-задачи и подключать суб-агентов, а не держать "
    "всё в одном сообщении."
)

try:
    agent._ANSWER_SYSTEM = persona.ANSWER_SYSTEM  # type: ignore[attr-defined]
    if hasattr(agent, "_ROLE") and persona.PERSONA_CORE not in agent._ROLE:  # type: ignore[attr-defined]
        agent._ROLE = persona.PERSONA_CORE + "\n" + agent._ROLE  # type: ignore[attr-defined]
    if hasattr(agent, "_ALGORITHM") and _NATIVE_MANDATE not in agent._ALGORITHM:  # type: ignore[attr-defined]
        agent._ALGORITHM = _NATIVE_MANDATE + "\n" + agent._ALGORITHM  # type: ignore[attr-defined]
except Exception as exc:  # noqa: BLE001
    log.debug("persona/native mandate patch skipped: %s", exc)

try:
    from . import native_ops
    native_ops.register(agent._registry)  # type: ignore[attr-defined]
    log.info("Native host tools registered: native_host/native_window/native_ui")
except Exception as exc:  # noqa: BLE001
    log.debug("native tools registration skipped: %s", exc)

try:
    from . import mission_ops
    mission_ops.register(agent._registry)  # type: ignore[attr-defined]
    log.info("Mission autonomy tool registered: mission")
except Exception as exc:  # noqa: BLE001
    log.debug("mission tool registration skipped: %s", exc)

_CLUSTER_ENABLED = os.environ.get("JARVIS_CLUSTER_ENABLE", "0") == "1"
if _CLUSTER_ENABLED:
    try:
        from cognitive_core import subagents as _cc_subagents
        from .cluster import cluster_router
        _raw_cc_run_role = _cc_subagents.run_role

        async def _cluster_run_role(role: str, task: str, *, chat: Any, context: str = "") -> dict[str, Any]:
            if os.environ.get("JARVIS_CLUSTER_ENABLE", "0") == "1":
                messages = [{"role": "system", "content": f"Ты — {role}-Agent JARVIS. Верни краткий рабочий brief для Core JARVIS."}]
                if context:
                    messages.append({"role": "system", "content": "Контекст:\n" + context})
                messages.append({"role": "user", "content": task})
                try:
                    res = await cluster_router.offload_chat(messages, role=role, max_tokens=1200)
                    if res.get("ok"):
                        return {"ok": True, "role": role, "content": res.get("content", ""), "node": res.get("node"), "offloaded": True}
                except Exception as exc:  # noqa: BLE001
                    log.debug("cluster offload skipped for role=%s: %s", role, exc)
            return await _raw_cc_run_role(role, task, chat=chat, context=context)

        _cc_subagents.run_role = _cluster_run_role
        log.info("Cognitive sub-agent cluster offload enabled.")
    except Exception as exc:  # noqa: BLE001
        log.debug("sub-agent cluster patch skipped: %s", exc)
else:
    log.info("Cluster offload disabled (JARVIS_CLUSTER_ENABLE=0).")


def _strip_outer_quotes(text: str) -> str:
    text = (text or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"`":
        text = text[1:-1].strip()
    return text


def _extract_powershell_payload(command: str) -> str:
    """powershell -NoExit -Command "..." → только полезная команда без оболочки."""
    text = (command or "").strip()
    m = re.match(r"(?is)^\s*(?:powershell|pwsh)(?:\.exe)?\s+(.*)$", text)
    if not m:
        return text
    tail = m.group(1).strip()
    cm = re.search(r"(?is)(?:--%\s+)?(?:-|/)c(?:ommand)?\s+(.+)$", tail)
    if cm:
        return _strip_outer_quotes(cm.group(1))
    # Без явного -Command: убираем типовые флаги оболочки, остальное считаем командой.
    tail = re.sub(r"(?is)(?:^|\s)-(?:NoExit|NoProfile|NonInteractive|ExecutionPolicy)\b(?:\s+\S+)?", " ", tail)
    return _strip_outer_quotes(tail.strip()) or text


def _coerce_windows_args(args: Any) -> dict[str, Any]:
    """
    Защитная нормализация Windows-вызовов.

    Gemma иногда отдаёт `action=windows` и строковый input вида
    `powershell -NoExit -Command "..."`. Старый handler видел action='' и падал
    «Неизвестное действие». Здесь превращаем такие формы в валидный вызов.
    """
    if not isinstance(args, dict):
        args = {"command": str(args or "")}
    args = dict(args or {})
    action = str(args.get("action") or "").strip()
    command = str(args.get("command") or args.get("cmd") or args.get("value") or "").strip()

    if not action and command:
        low = command.lower().strip()
        if re.match(r"^(powershell|pwsh)(\.exe)?\b", low):
            args["action"] = "powershell"
            args["command"] = _extract_powershell_payload(command)
        elif low.startswith(("cmd /k", "cmd.exe /k")):
            args["action"] = "open_app"
            args["command"] = command
        else:
            args["action"] = "exec"
            args["command"] = command
        return args

    if action in ("powershell", "exec") and command and re.match(r"(?is)^\s*(powershell|pwsh)(\.exe)?\b", command):
        args["action"] = "powershell"
        args["command"] = _extract_powershell_payload(command)
    return args


try:
    _raw_normalize_plan = agent._normalize_plan  # type: ignore[attr-defined]

    def _normalize_plan_with_windows_repair(action: str, args: Any) -> tuple[str, dict[str, Any]]:
        normalized_action, normalized_args = _raw_normalize_plan(action, args)
        if normalized_action == "windows":
            normalized_args = _coerce_windows_args(normalized_args)
        return normalized_action, normalized_args

    agent._normalize_plan = _normalize_plan_with_windows_repair  # type: ignore[attr-defined]

    _windows_tool = agent._registry.get("windows")  # type: ignore[attr-defined]
    if _windows_tool is not None:
        _raw_windows_handler = _windows_tool.handler

        async def _windows_handler_with_repair(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return await _raw_windows_handler(_coerce_windows_args(args), ctx)

        _windows_tool.handler = _windows_handler_with_repair
except Exception as exc:  # noqa: BLE001
    log.debug("windows tool repair patch skipped: %s", exc)

_raw_reset_context = agent.reset_context
_raw_run_chat = agent.run_chat
_raw_skills_overview = agent.skills_overview

_background_lock: asyncio.Lock | None = None
_background_started = False
_gpu_guard: Any | None = None
_idle_loop: Any | None = None


async def _purge_episodic_trace(session_id: str) -> None:
    try:
        from cognitive_core import db as cc_db
        await cc_db.execute("DELETE FROM episodic_memory_logs WHERE session_id = ?", (session_id,))
    except Exception as exc:  # noqa: BLE001
        log.debug("episodic trace purge skipped for session=%s: %s", session_id, exc)


def reset_context(session_id: str = "default", keep_summary: bool = False) -> None:
    _raw_reset_context(session_id, keep_summary=keep_summary)
    try:
        asyncio.get_running_loop().create_task(_purge_episodic_trace(session_id))
    except RuntimeError:
        pass


def _host_exec_adapter(bridge: Any | None):
    if bridge is None:
        return None

    async def _exec(command: str) -> dict[str, Any]:
        try:
            res = await bridge.call("exec", {"command": command}, timeout=60)
            result = (res or {}).get("result", {}) or {}
            out = (result.get("stdout") or "") + (result.get("stderr") or "")
            return {"ok": bool((res or {}).get("ok")), "out": out, "code": result.get("returncode")}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "out": str(exc), "code": None}

    return _exec


async def ensure_background_runtime(bridge: Any | None = None) -> dict[str, Any]:
    global _background_lock, _background_started, _gpu_guard, _idle_loop
    if os.environ.get("JARVIS_BACKGROUND_RUNTIME", "1") == "0":
        return {"enabled": False, "reason": "JARVIS_BACKGROUND_RUNTIME=0"}
    if _background_lock is None:
        _background_lock = asyncio.Lock()
    async with _background_lock:
        if _background_started:
            return background_status()
        try:
            from .gpu_guard import GpuGuard
            from .idle_loop import BackgroundIdleLoop

            host_exec = _host_exec_adapter(bridge)
            _gpu_guard = GpuGuard(host_exec=host_exec)
            await _gpu_guard.start()
            _idle_loop = BackgroundIdleLoop(host_exec=host_exec, broadcast=None, gpu_guard=_gpu_guard)
            await _idle_loop.start()
            _background_started = True
            log.info("Background runtime запущен: GPU guard + idle diagnostics loop.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Background runtime не запущен")
            return {"enabled": False, "error": str(exc)}
    return background_status()


def background_status() -> dict[str, Any]:
    return {
        "enabled": _background_started,
        "gpu_guard": _gpu_guard.status() if _gpu_guard is not None else None,
        "idle_loop": _idle_loop.status() if _idle_loop is not None else None,
    }


async def stop_background_runtime() -> None:
    global _background_started, _gpu_guard, _idle_loop
    for obj in (_idle_loop, _gpu_guard):
        if obj is None:
            continue
        try:
            await obj.stop()
        except Exception:  # noqa: BLE001
            pass
    _idle_loop = None
    _gpu_guard = None
    _background_started = False


async def run_chat(session_id: str, user_text: str, bridge: Optional[Any] = None) -> AsyncIterator[dict[str, Any]]:
    _sync_runtime_flags()
    await ensure_background_runtime(bridge)
    if _idle_loop is not None:
        _idle_loop.mark_user_activity(active=True)
    try:
        async for ev in _raw_run_chat(session_id, user_text, bridge=bridge):
            yield ev
    finally:
        if _idle_loop is not None:
            _idle_loop.mark_user_activity(active=False)


async def run_task(task: str, bridge: Optional[Any] = None) -> AsyncIterator[dict[str, Any]]:
    async for ev in run_chat("default", task, bridge=bridge):
        yield {"channel": "chat", **ev}


def skills_overview() -> dict[str, Any]:
    data = _raw_skills_overview()
    data["runtime"] = background_status()
    data["native_tools"] = ["native_host", "native_window", "native_ui"]
    data["mission_tool"] = "mission"
    data["cluster"] = (cluster_router.status() if _CLUSTER_ENABLED and "cluster_router" in globals()
                       else {"enabled": False, "reason": "JARVIS_CLUSTER_ENABLE=0"})
    return data


agent.reset_context = reset_context
agent.run_chat = run_chat
agent.run_task = run_task
agent.skills_overview = skills_overview

memory_overview = agent.memory_overview
flush_context = agent.flush_context
clear_longterm = agent.clear_longterm
save_memory = agent.save_memory
incident_overview = agent.incident_overview
clear_incidents = agent.clear_incidents

__all__ = [
    "run_chat", "run_task", "memory_overview", "flush_context", "reset_context",
    "clear_longterm", "save_memory", "incident_overview", "clear_incidents",
    "skills_overview", "ensure_background_runtime", "background_status",
    "stop_background_runtime",
]
