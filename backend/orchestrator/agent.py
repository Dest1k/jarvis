# -*- coding: utf-8 -*-
"""
agent.py — Core JARVIS orchestration layer.

Refactor goals:
• Core Identity is always JARVIS; specialised roles operate behind the curtain.
• Planner stays JSON-only for robust vLLM operation.
• Sub-agents produce short briefs for the Core Agent; they never speak directly to
  the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from . import llm
from .memory import ConversationManager, LongTermMemory
from .persona import answer_system, planner_system, subagent_system
from .tools import Tool, ToolContext, ToolRegistry

log = logging.getLogger("jarvis.agent")

MAX_STEPS = int(os.environ.get("JARVIS_AGENT_MAX_STEPS", "8"))
SUBAGENTS_ENABLED = os.environ.get("JARVIS_ENABLE_SUBAGENTS", "1") != "0"
SUBAGENT_TIMEOUT = int(os.environ.get("JARVIS_SUBAGENT_TIMEOUT", "90"))

_longterm = LongTermMemory()
_SOFT_BUDGET = max(2500, int(llm.AGENT_INPUT_BUDGET * 0.5))
_conversations = ConversationManager(_longterm, soft_budget_tokens=_SOFT_BUDGET)
_registry = ToolRegistry()

_TOOL_ACTOR = {
    "gui": "ui-tars",
    "run_code": "sandbox", "shell": "sandbox",
    "windows": "host", "open_url": "host", "list_dir": "host", "system_info": "host",
    "web_fetch": "web", "web_search": "web", "weather": "web", "wikipedia": "web",
    "http_request": "web", "exchange_rate": "web", "define": "web", "translate": "web",
    "memory_save": "memory", "memory_search": "memory", "list_memory": "memory",
    "calculator": "local", "now": "local",
}


def _actor_for(tool: str) -> str:
    if tool.startswith("mcp_"):
        return "mcp"
    return _TOOL_ACTOR.get(tool, "host")


# --------------------------------------------------------------------------- #
# MCP dynamic tool layer
# --------------------------------------------------------------------------- #
def _register_mcp_tool(qual: str, description: str, schema: dict[str, Any], server: str) -> None:
    params: dict[str, str] = {}
    for k, v in (schema or {}).get("properties", {}).items():
        params[k] = str(v.get("description") or v.get("type") or "")[:80]

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        from . import mcp_client
        return await mcp_client.mcp_manager.call(qual, args)

    _registry.add(Tool(qual, f"[MCP:{server}] {description}".strip()[:280], params, handler))


async def start_mcp() -> None:
    try:
        from . import mcp_client
        await mcp_client.mcp_manager.start(_register_mcp_tool)
    except Exception:  # noqa: BLE001
        log.exception("MCP не инициализирован (продолжаю без него)")


async def stop_mcp() -> None:
    try:
        from . import mcp_client
        await mcp_client.mcp_manager.stop()
    except Exception:  # noqa: BLE001
        pass


def mcp_status() -> dict[str, Any]:
    try:
        from . import mcp_client
        return mcp_client.mcp_manager.status()
    except Exception:  # noqa: BLE001
        return {"servers": {}, "tool_count": 0, "tools": []}


# --------------------------------------------------------------------------- #
# Core + sub-agent orchestration
# --------------------------------------------------------------------------- #
def _roles_for(text: str) -> list[str]:
    low = (text or "").lower()
    roles: list[str] = []
    if any(w in low for w in ("код", "bug", "ошибка", "traceback", "рефактор", "патч", "test", "build")):
        roles.append("coder")
    if any(w in low for w in ("windows", "wmi", "win32", "docker", "gpu", "vram", "сервис", "сеть", "wsl", "nvml")):
        roles.append("sysadmin")
    if any(w in low for w in ("найди", "исслед", "research", "источник", "web", "osint", "документац")):
        roles.append("researcher")
    if roles:
        roles.append("critic")
    return list(dict.fromkeys(roles))[:4]


async def _consult_subagents(session_id: str, user_text: str) -> list[dict[str, str]]:
    if not SUBAGENTS_ENABLED:
        return []
    roles = _roles_for(user_text)
    if not roles:
        return []
    history = _conversations.build_context(session_id, min(4000, llm.AGENT_INPUT_BUDGET // 2))

    async def run_role(role: str) -> dict[str, str]:
        messages = [
            {"role": "system", "content": subagent_system(role)},
            *history,
            {"role": "user", "content": (
                "Выполни внутренний анализ для Core JARVIS. Верни краткий JSON или Markdown brief: "
                "findings, actions, risks, tests/verification. Запрос:\n" + user_text
            )},
        ]
        try:
            brief = await asyncio.wait_for(
                llm.chat(messages, temperature=0.15, max_tokens=900, timeout=SUBAGENT_TIMEOUT),
                timeout=SUBAGENT_TIMEOUT + 5,
            )
            return {"role": role, "brief": brief.strip()[:3500]}
        except Exception as exc:  # noqa: BLE001
            return {"role": role, "brief": f"sub-agent unavailable: {exc}"}

    return await asyncio.gather(*(run_role(r) for r in roles))


async def _summarize(text: str) -> str:
    messages = [
        {"role": "system", "content": "Сожми диалог в краткую фактологическую сводку на русском: имена, цели, решения, незакрытые задачи. До 200 слов."},
        {"role": "user", "content": text},
    ]
    return await llm.chat(messages, temperature=0.2, max_tokens=512, timeout=120)


def _scratch_to_text(scratch: list[dict[str, Any]]) -> str:
    if not scratch:
        return ""
    parts: list[str] = []
    for i, s in enumerate(scratch, 1):
        args = json.dumps(s.get("args", {}), ensure_ascii=False)
        parts.append(f"[Шаг {i}] action={s.get('action')} input={args}\nнаблюдение: {s.get('observation', '')}")
    return "\n\n".join(parts)


def _briefs_to_text(briefs: list[dict[str, str]]) -> str:
    if not briefs:
        return ""
    return "\n\n".join(f"[{b['role'].upper()}]\n{b['brief']}" for b in briefs)


async def _plan_next(session_id: str, scratch: list[dict[str, Any]], briefs: list[dict[str, str]]) -> dict[str, Any]:
    scratch_text = _scratch_to_text(scratch)
    brief_text = _briefs_to_text(briefs)
    system = planner_system(_registry.specs())
    reserve = llm.estimate_tokens(system) + llm.estimate_tokens(scratch_text) + llm.estimate_tokens(brief_text) + 500
    history_budget = max(1000, llm.AGENT_INPUT_BUDGET - reserve)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(_conversations.build_context(session_id, history_budget))
    if brief_text:
        messages.append({"role": "system", "content": "Внутренние brief'ы суб-агентов:\n" + brief_text})
    if scratch_text:
        messages.append({"role": "system", "content": "Уже собранные наблюдения инструментов:\n" + scratch_text})
    messages.append({"role": "user", "content": "Каков следующий безопасный шаг? Ответь только JSON."})

    raw = await llm.chat(messages, temperature=0.1, max_tokens=512, timeout=120)
    parsed = llm.extract_json(raw)
    if not parsed or "action" not in parsed:
        return {"action": "answer", "thought": "Перехожу к аккуратному финальному ответу."}
    return parsed


async def _answer_messages(session_id: str, scratch: list[dict[str, Any]], briefs: list[dict[str, str]]) -> list[dict[str, Any]]:
    scratch_text = _scratch_to_text(scratch)
    brief_text = _briefs_to_text(briefs)
    system = answer_system()
    reserve = llm.estimate_tokens(system) + llm.estimate_tokens(scratch_text) + llm.estimate_tokens(brief_text) + 400
    history_budget = max(1000, llm.AGENT_INPUT_BUDGET - reserve)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(_conversations.build_context(session_id, history_budget))
    if brief_text:
        messages.append({"role": "system", "content": "Внутренние brief'ы суб-агентов. Используй, но отвечай только как JARVIS:\n" + brief_text})
    if scratch_text:
        messages.append({"role": "system", "content": "Наблюдения инструментов. Опирайся строго на них:\n" + scratch_text})
    return messages


async def run_chat(session_id: str, user_text: str, bridge: Optional[Any] = None) -> AsyncIterator[dict[str, Any]]:
    user_text = (user_text or "").strip()
    if not user_text:
        yield {"type": "error", "error": "Пустое сообщение."}
        return

    _conversations.add(session_id, "user", user_text)
    ctx = ToolContext(bridge=bridge, longterm=_longterm, session_id=session_id)
    scratch: list[dict[str, Any]] = []
    seen_calls: set[str] = set()

    briefs = await _consult_subagents(session_id, user_text)
    for b in briefs:
        # Прозрачная, но не шумная трассировка: видно, что роль работала, без раскрытия длинного brief.
        yield {"type": "thought", "actor": b["role"], "text": f"{b['role'].title()}-Agent подготовил внутренний brief."}

    try:
        for _ in range(MAX_STEPS):
            plan = await _plan_next(session_id, scratch, briefs)
            action = str(plan.get("action", "answer")).strip()
            thought = str(plan.get("thought", "")).strip()
            if thought:
                yield {"type": "thought", "text": thought, "actor": "dispatcher"}
            if action in ("answer", "final", "respond", "done", ""):
                break
            tool = _registry.get(action)
            if tool is None:
                scratch.append({"action": action, "args": {}, "observation": f"Нет такого инструмента '{action}'."})
                continue
            args = plan.get("action_input") or plan.get("input") or {}
            if not isinstance(args, dict):
                args = {"value": args}
            call_key = action + "::" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if action != "gui" and call_key in seen_calls:
                yield {"type": "thought", "actor": "dispatcher", "text": "Этот шаг уже выполнен; перехожу к ответу."}
                break
            seen_calls.add(call_key)
            actor = _actor_for(action)
            yield {"type": "tool_call", "tool": action, "args": args, "actor": actor}
            result = await _registry.run(action, args, ctx)
            observation = result.get("content", "")
            yield {"type": "tool_result", "tool": action, "actor": actor, "ok": bool(result.get("ok")), "summary": observation[:600]}
            scratch.append({"action": action, "args": args, "observation": observation})
        else:
            yield {"type": "thought", "actor": "dispatcher", "text": f"Достигнут лимит шагов ({MAX_STEPS}); формирую ответ."}
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой фазы планирования")
        yield {"type": "thought", "actor": "dispatcher", "text": f"Ошибка планирования: {exc}. Отвечаю напрямую."}

    yield {"type": "assistant_start", "actor": "dispatcher"}
    final_text = ""
    try:
        messages = await _answer_messages(session_id, scratch, briefs)
        async for delta in llm.chat_stream(messages, temperature=0.35, max_tokens=2048):
            final_text += delta
            yield {"type": "token", "content": delta}
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой фазы ответа")
        if not final_text:
            final_text = f"Сэр, ответная модель споткнулась о ковёр инфраструктуры: {exc}"
            yield {"type": "token", "content": final_text}

    if not final_text.strip():
        final_text = "Готово, сэр. Скромно, но эффективно."
        yield {"type": "token", "content": final_text}

    _conversations.add(session_id, "assistant", final_text)
    yield {"type": "assistant_done", "content": final_text}

    try:
        if await _conversations.maybe_summarize(session_id, _summarize):
            yield {"type": "memory", "event": "summarized", "text": "Контекст сжат и сохранён в память."}
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Dashboard-compatible memory/control API
# --------------------------------------------------------------------------- #
def memory_overview(session_id: str = "default") -> dict[str, Any]:
    conv = _conversations.get(session_id)
    return {
        "session": session_id,
        "summary": conv.summary,
        "recent_count": len(conv.messages),
        "longterm": _longterm.all()[:50],
        "longterm_count": len(_longterm.all()),
    }


def reset_context(session_id: str = "default", keep_summary: bool = False) -> None:
    _conversations.reset(session_id, keep_summary=keep_summary)


def mark_interrupted(session_id: str = "default") -> None:
    conv = _conversations.get(session_id)
    if conv.messages and conv.messages[-1]["role"] == "user":
        _conversations.add(session_id, "assistant", "(Задача прервана пользователем. Жду новый запрос.)")


async def flush_context(session_id: str = "default") -> bool:
    return await _conversations.flush(session_id, _summarize)


def clear_longterm() -> int:
    return _longterm.clear()


def save_memory(text: str, tags: Optional[list[str]] = None) -> dict[str, Any]:
    return _longterm.save(text, tags=tags, kind="fact")


async def run_task(task: str, bridge: Optional[Any] = None) -> AsyncIterator[dict[str, Any]]:
    async for ev in run_chat("default", task, bridge=bridge):
        yield {"channel": "chat", **ev}
