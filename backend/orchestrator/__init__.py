# -*- coding: utf-8 -*-
"""
Пакет оркестрации JARVIS-OS — агентская «прокладка» между запросом пользователя
(чат/голос) и «мозгом» системы (Qwen + UI-TARS).

Состав:
    llm.py     — клиент vLLM + бюджет контекста + извлечение JSON.
    memory.py  — долговременная память + менеджер оперативного контекста.
    tools.py   — реестр инструментов (код, Windows, GUI, веб, погода, память).
    agent.py   — ReAct-оркестратор (планирование инструментами → потоковый ответ).
    graph.py   — слой совместимости (ре-экспорт run_task/run_chat).
"""

from .agent import (  # noqa: F401
    run_chat,
    run_task,
    memory_overview,
    reset_context,
    flush_context,
    clear_longterm,
    save_memory,
)
