# -*- coding: utf-8 -*-
"""
graph.py — тонкий слой совместимости.

Прежняя наивная реализация (dispatch→coder/os→finalize без реального tool-use)
заменена полноценным ReAct-оркестратором в `agent.py`. Этот модуль сохранён,
чтобы не ломать существующие импорты (`from orchestrator.graph import run_task`).
Вся логика теперь живёт в `orchestrator.agent`.
"""

from __future__ import annotations

from .agent import run_chat, run_task  # noqa: F401  (ре-экспорт)

__all__ = ["run_task", "run_chat"]
