# -*- coding: utf-8 -*-
"""
Пакет оркестрации JARVIS-OS — агентская «прокладка» между запросом пользователя
(чат/голос) и «мозгом» системы (Qwen + UI-TARS).

Состав:
    fsio.py      — атомарный UTF-8/LF файловый ввод-вывод (инженерный стандарт v2.0).
    llm.py       — клиент vLLM + бюджет контекста + извлечение JSON + ретраи.
    memory.py    — долговременная память + менеджер оперативного контекста.
    incidents.py — журнал решённых инцидентов (эпистемическое логирование, v2.0).
    skills.py    — кузница навыков (рутины → CLI-скрипты + индекс, v2.0).
    git_intel.py — Git-интеллект (разведка веток, diff-оценка, перенос, v2.0).
    inference.py — двухрежимный инференс (MoE-турбо / dense-гибрид Gemma 4, v2.0).
    media.py     — мультимедиа HEVC/H.265 с аппаратным ускорением (v2.0).
    tools.py     — реестр инструментов (код, Windows, GUI, веб, git, навыки, память).
    persona.py   — каноническая личность Core JARVIS и стиль ответов.
    agent.py     — ReAct-оркестратор (планирование инструментами → потоковый ответ).
    graph.py     — слой совместимости (ре-экспорт run_task/run_chat).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Optional

from . import agent as agent  # noqa: F401
from . import persona

log = logging.getLogger("jarvis.orchestrator")

# Apply the canonical Core JARVIS personality without rewriting the large agent.py.
# agent.py still owns the operational playbooks; persona.py owns the public style.
try:
    agent._ANSWER_SYSTEM = persona.ANSWER_SYSTEM  # type: ignore[attr-defined]
    if hasattr(agent, "_ROLE") and persona.PERSONA_CORE not in agent._ROLE:  # type: ignore[attr-defined]
        agent._ROLE = persona.PERSONA_CORE + "\n" + agent._ROLE  # type: ignore[attr-defined]
except Exception as exc:  # noqa: BLE001
    log.debug("persona patch skipped: %s", exc)

_raw_reset_context = agent.reset_context
_raw_run_chat = agent.run_chat

_background_lock: asyncio.Lock | None = None
_background_started = False
_gpu_guard: Any | None = None
_idle_loop: Any | None = None


async def _purge_episodic_trace(session_id: str) -> None:
    """Очистить runtime explainability trace для вкладки.

    Важно: «почему?» в dashboard берётся не из ConversationManager, а из
    cognitive_core.episodic_memory_logs. Поэтому reset_context обязан очищать и
    эту таблицу по session_id, иначе backend уже забыл контекст, а UI снова
    достаёт старую трассу из БД.
    """
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
    """Запустить безопасный background runtime-loop один раз.

    Подключается лениво при первом пользовательском ходе, потому что именно тогда
    в orchestrator есть bridge-объект. Без bridge guard/idle-loop остаются в
    диагностическом режиме без host remediation.
    """
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
    """Совместимый генератор: оборачивает run_chat, проставляя channel=chat."""
    async for ev in run_chat("default", task, bridge=bridge):
        yield {"channel": "chat", **ev}


agent.reset_context = reset_context

memory_overview = agent.memory_overview
flush_context = agent.flush_context
clear_longterm = agent.clear_longterm
save_memory = agent.save_memory
incident_overview = agent.incident_overview
clear_incidents = agent.clear_incidents
skills_overview = agent.skills_overview

__all__ = [
    "run_chat", "run_task", "memory_overview", "flush_context", "reset_context",
    "clear_longterm", "save_memory", "incident_overview", "clear_incidents",
    "skills_overview", "ensure_background_runtime", "background_status",
    "stop_background_runtime",
]
