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

from .agent import (  # noqa: F401
    run_chat,
    run_task,
    memory_overview,
    reset_context,
    flush_context,
    clear_longterm,
    save_memory,
    incident_overview,
    clear_incidents,
    skills_overview,
)
