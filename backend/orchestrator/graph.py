# -*- coding: utf-8 -*-
"""
graph.py — тонкий слой совместимости.

Прежняя наивная реализация (dispatch→coder/os→finalize без реального tool-use)
заменена полноценным ReAct-оркестратором. Экспорт идёт через пакетный entrypoint
`orchestrator.__init__`, чтобы background runtime wrapper (GPU guard / idle loop)
и очистка episodic trace применялись даже для старого импорта:
`from orchestrator.graph import run_task`.
"""

from __future__ import annotations

from . import run_chat, run_task  # noqa: F401

__all__ = ["run_task", "run_chat"]
