# -*- coding: utf-8 -*-
"""
cognitive_core — «Когнитивное ядро» JARVIS OS (production-grade foundation).

Слои (все блокирующие операции — вне event loop):
    schema.sql — реляционная схема (SQLite→PostgreSQL-совместимая): состояние
                 автомата, настройки/промпты (DB>file), реестр файлов, граф
                 знаний с эмбеддингами, эпизодическая память, достижения,
                 аудит с откатом, снимки здоровья, планы проектов, RBAC.
    db.py      — async-доступ к БД (sqlite3 в ThreadPoolExecutor) + аудит/откат.
    config.py  — приоритет конфигурации: БД (is_active) перекрывает файлы.
    models.py  — pydantic v2 модели (быстрая сериализация для WS/фронта).
    suspend.py — Zero-Latency Suspend/Resume фонового цикла (обрыв стрима vLLM).
    plugins.py — горячие плагины-навыки с Critic-валидацией и HITL по danger.

Полная архитектура (суб-агенты, RAG-ingestion, self-healing, Next.js-контракты,
PWA-голос) — в docs/cognitive_core_architecture.md.
"""

from __future__ import annotations

import os
from pathlib import Path

# Persist by default inside the mounted backend data volume. Operators may still
# override JARVIS_DB_PATH explicitly. This aligns cognitive_core, SQLite MCP and
# dashboard DB browser on one durable database location.
if "JARVIS_DB_PATH" not in os.environ:
    data_memory = Path("/data/memory")
    os.environ["JARVIS_DB_PATH"] = str(data_memory / "cognitive_core.db") if data_memory.exists() else str(Path("./.jarvis_core") / "cognitive_core.db")

from . import (config, db, executor, federation, ingest, jsonx,  # noqa: E402,F401
               learning, maintenance, models, parallel, plugins, recovery,
               subagents, suspend)

__all__ = ["db", "config", "models", "suspend", "plugins", "ingest",
           "subagents", "maintenance", "learning", "executor", "recovery",
           "federation", "parallel", "jsonx"]
