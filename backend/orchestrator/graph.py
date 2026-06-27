# -*- coding: utf-8 -*-
"""
graph.py — граф состояний мультиагентной оркестрации JARVIS-OS на базе LangGraph.

Архитектура графа:
    ┌──────────┐   маршрутизация   ┌──────────────┐
    │ dispatch │ ────────────────► │ coder_agent  │ ──► sandbox (исполнение кода)
    │ (Qwen)   │ ────────────────► │ os_agent     │ ──► RPC-bridge / UI-TARS
    └──────────┘                   └──────────────┘
         │                                │
         └────────────► finalize ◄────────┘

Диспетчер (Qwen2.5-Coder-32B) классифицирует задачу и направляет её
на кодер-агента (кодинг в dockerized-sandbox) либо на ОС-агента
(управление GUI через UI-TARS и нативные хуки через RPC-мост).

Граф спроектирован как асинхронный генератор событий: каждое значимое
состояние отдаётся наружу для стриминга в дашборд (каналы chat/deploy).

Зависимости: langgraph, langchain-openai (или прямые httpx-запросы к vLLM).
Реализация ниже использует прямые httpx-вызовы к vLLM, чтобы не зависеть
от конкретной версии langchain, но структура совместима с StateGraph.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional, TypedDict

import httpx

QWEN_URL = os.environ.get("JARVIS_QWEN_URL", "http://vllm-qwen-coder:8001/v1")
UITARS_URL = os.environ.get("JARVIS_UITARS_URL", "http://vllm-ui-tars:8002/v1")
SANDBOX_CONTAINER = os.environ.get("JARVIS_SANDBOX_CONTAINER", "jarvis-sandbox")


class AgentState(TypedDict, total=False):
    """Состояние, протекающее через узлы графа."""
    task: str
    route: str            # 'coder' | 'os' | 'chat'
    plan: str
    code: str
    exec_result: dict[str, Any]
    os_actions: list[dict[str, Any]]
    final: str


# --------------------------------------------------------------------------- #
# Низкоуровневый вызов vLLM
# --------------------------------------------------------------------------- #
async def _complete(base_url: str, model: str, system: str, user: str,
                    temperature: float = 0.2, max_tokens: int = 2048) -> str:
    """Неблокирующий не-стриминговый вызов чат-комплишена к vLLM."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{base_url}/chat/completions", json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# Узел 1. Диспетчер — классификация и маршрутизация задачи
# --------------------------------------------------------------------------- #
async def node_dispatch(state: AgentState) -> AgentState:
    """Определить маршрут выполнения задачи."""
    system = (
        "Ты — диспетчер мультиагентной системы JARVIS-OS. Классифицируй задачу "
        "пользователя строго в один из маршрутов и верни JSON вида "
        '{"route": "coder|os|chat", "plan": "краткий план на русском"}. '
        "coder — написание/правка/запуск кода; os — управление окнами, мышью, "
        "приложениями Windows; chat — обычный ответ."
    )
    raw = await _complete(QWEN_URL, "qwen-coder", system, state["task"], temperature=0.1)
    try:
        parsed = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        state["route"] = parsed.get("route", "chat")
        state["plan"] = parsed.get("plan", "")
    except (json.JSONDecodeError, ValueError):
        state["route"] = "chat"
        state["plan"] = raw[:400]
    return state


# --------------------------------------------------------------------------- #
# Узел 2. Кодер-агент — генерация кода и исполнение в sandbox
# --------------------------------------------------------------------------- #
async def node_coder(state: AgentState) -> AgentState:
    """Сгенерировать код по задаче и исполнить его в изолированном контейнере."""
    system = (
        "Ты — кодер-агент JARVIS-OS на базе Qwen2.5-Coder. Сгенерируй ПОЛНЫЙ "
        "исполнимый Python-код для решения задачи. Только код, без пояснений."
    )
    code = await _complete(QWEN_URL, "qwen-coder", system,
                           f"{state['plan']}\n\nЗадача: {state['task']}",
                           temperature=0.2, max_tokens=4096)
    # Срезаем markdown-ограждения, если есть
    if "```" in code:
        parts = code.split("```")
        code = parts[1] if len(parts) > 1 else code
        if code.startswith("python"):
            code = code[len("python"):]
    state["code"] = code.strip()
    state["exec_result"] = await _run_in_sandbox(state["code"])
    return state


async def _run_in_sandbox(code: str) -> dict[str, Any]:
    """Исполнить код в изолированном docker-контейнере sandbox."""
    import asyncio
    import base64

    b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    # Декодируем и исполняем внутри sandbox без проброса хост-ФС
    inner = f"echo {b64} | base64 -d > /tmp/task.py && timeout 60 python /tmp/task.py"
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", SANDBOX_CONTAINER, "bash", "-lc", inner,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return {
        "returncode": proc.returncode,
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
    }


# --------------------------------------------------------------------------- #
# Узел 3. ОС-агент — управление GUI через UI-TARS + RPC-мост
# --------------------------------------------------------------------------- #
async def node_os(state: AgentState, bridge: Optional[Any] = None) -> AgentState:
    """Спланировать действия GUI через UI-TARS и выполнить их через RPC-мост."""
    system = (
        "Ты — UI-TARS, контроллер GUI. По задаче верни JSON-массив действий вида "
        '[{"action":"open_app|exec|click|type","params":{...}}]. '
        "Действия должны быть атомарными и безопасными."
    )
    raw = await _complete(UITARS_URL, "ui-tars", system, state["task"],
                          temperature=0.0, max_tokens=1024)
    try:
        actions = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
    except (json.JSONDecodeError, ValueError):
        actions = []
    state["os_actions"] = actions

    # Исполнение через RPC-мост (с HITL для деструктивных действий)
    if bridge is not None:
        for act in actions:
            await bridge.call(act.get("action", "exec"), act.get("params", {}))
    return state


# --------------------------------------------------------------------------- #
# Узел 4. Финализация
# --------------------------------------------------------------------------- #
async def node_finalize(state: AgentState) -> AgentState:
    """Сформировать итоговый ответ пользователю на русском."""
    summary_parts = [f"План: {state.get('plan', '')}"]
    if state.get("exec_result"):
        rc = state["exec_result"].get("returncode")
        summary_parts.append(f"Исполнение кода: код возврата {rc}.")
    if state.get("os_actions"):
        summary_parts.append(f"Выполнено GUI-действий: {len(state['os_actions'])}.")
    state["final"] = "\n".join(summary_parts)
    return state


# --------------------------------------------------------------------------- #
# Оркестрация графа (асинхронный генератор событий для стриминга)
# --------------------------------------------------------------------------- #
async def run_task(task: str, bridge: Optional[Any] = None) -> AsyncIterator[dict[str, Any]]:
    """
    Прогнать задачу по графу состояний, отдавая события в дашборд.

    Совместимо с LangGraph StateGraph: узлы можно зарегистрировать как
    add_node(...) и связать рёбрами по полю state['route']. Здесь дан
    эквивалентный явный поток для прозрачности и независимости от версии.
    """
    state: AgentState = {"task": task}

    yield {"channel": "chat", "type": "status", "stage": "dispatch",
           "message": "Диспетчеризация задачи…"}
    state = await node_dispatch(state)
    yield {"channel": "chat", "type": "route", "route": state["route"],
           "plan": state.get("plan", "")}

    if state["route"] == "coder":
        yield {"channel": "deploy", "type": "status", "stage": "coding",
               "message": "Кодер-агент генерирует и исполняет код…"}
        state = await node_coder(state)
        yield {"channel": "code", "type": "code_diff", "code": state.get("code", ""),
               "exec_result": state.get("exec_result", {})}
    elif state["route"] == "os":
        yield {"channel": "chat", "type": "status", "stage": "os_control",
               "message": "ОС-агент (UI-TARS) управляет десктопом…"}
        state = await node_os(state, bridge=bridge)
        yield {"channel": "chat", "type": "os_actions",
               "actions": state.get("os_actions", [])}

    state = await node_finalize(state)
    yield {"channel": "chat", "type": "final", "content": state["final"]}
