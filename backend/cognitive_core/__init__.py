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

from . import config, db, ingest, models, plugins, suspend  # noqa: F401

__all__ = ["db", "config", "models", "suspend", "plugins", "ingest"]
