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
    agent.py     — ReAct-оркестратор (планирование инструментами → потоковый ответ).
    graph.py     — слой совместимости (ре-экспорт run_task/run_chat).
"""

from __future__ import annotations

import asyncio
import logging

from . import agent as agent  # noqa: F401

log = logging.getLogger("jarvis.orchestrator")

_raw_reset_context = agent.reset_context


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
        # Вне event loop чистка не критична: обычный backend-путь всегда в loop.
        pass


agent.reset_context = reset_context

run_chat = agent.run_chat
run_task = agent.run_task
memory_overview = agent.memory_overview
flush_context = agent.flush_context
clear_longterm = agent.clear_longterm
save_memory = agent.save_memory
incident_overview = agent.incident_overview
clear_incidents = agent.clear_incidents
skills_overview = agent.skills_overview
