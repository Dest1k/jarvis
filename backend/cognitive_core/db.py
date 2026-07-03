# -*- coding: utf-8 -*-
"""
db.py — асинхронный слой доступа к БД когнитивного ядра JARVIS OS.

Проектные решения:
    • SQLite через СТАНДАРТНЫЙ модуль sqlite3 (нулевые внешние зависимости),
      но ВСЕ блокирующие вызовы вынесены в выделенный ThreadPoolExecutor через
      run_in_executor — событийный цикл asyncio НИКОГДА не блокируется файловым
      I/O БД (требование «zero asyncio bottlenecks»).
    • Одно соединение на пул-поток (check_same_thread=False + сериализация через
      один рабочий поток) — простая и корректная модель для SQLite.
    • WAL-режим (в schema.sql) даёт конкурентное чтение при записи.
    • Каждая мутация может писать audit_changelog (before/after) — «git для
      разума агента»: полная история и откат.
    • Совместимо с миграцией на PostgreSQL: SQL держим в переносимом подмножестве,
      а точечные различия инкапсулированы здесь.

Публичный API — async: connect(), execute(), query(), query_one(),
mutate_with_audit(), rollback_change(), apply_schema().
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Sequence

# Путь к БД: том данных ядра (переживает рестарт), фолбэк — рядом с ядром.
DB_PATH = Path(os.environ.get(
    "JARVIS_DB_PATH",
    str(Path(os.environ.get("JARVIS_CORE_DIR", "./.jarvis_core")) / "cognitive_core.db")))
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Единственный рабочий поток → сериализация обращений к SQLite (безопасно и
# предсказуемо), при этом event loop свободен.
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-db")
_conn: Optional[sqlite3.Connection] = None


def _row_to_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _connect_sync() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


async def _run(fn, *args) -> Any:
    """Выполнить блокирующую функцию в БД-пуле (event loop не блокируется)."""
    return await asyncio.get_running_loop().run_in_executor(_pool, fn, *args)


async def connect() -> None:
    """Открыть соединение и применить схему (идемпотентно)."""
    global _conn

    def _op() -> None:
        global _conn
        if _conn is None:
            _conn = _connect_sync()
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        _conn.executescript(schema)
        _conn.commit()

    await _run(_op)


async def apply_schema() -> None:
    await connect()


async def close() -> None:
    def _op() -> None:
        global _conn
        if _conn is not None:
            _conn.close()
            _conn = None
    await _run(_op)


# --------------------------------------------------------------------------- #
# Базовые операции
# --------------------------------------------------------------------------- #
async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """INSERT/UPDATE/DELETE. Возвращает число затронутых строк."""
    def _op() -> int:
        assert _conn is not None, "БД не подключена (вызовите connect())"
        cur = _conn.execute(sql, tuple(params))
        _conn.commit()
        return cur.rowcount
    return await _run(_op)


async def insert_returning_id(sql: str, params: Sequence[Any] = ()) -> int:
    """INSERT, возвращающий lastrowid (для таблиц с AUTOINCREMENT-ключом)."""
    def _op() -> int:
        assert _conn is not None, "БД не подключена"
        cur = _conn.execute(sql, tuple(params))
        _conn.commit()
        return int(cur.lastrowid or 0)
    return await _run(_op)


async def query(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """SELECT → список dict-строк."""
    def _op() -> list[dict[str, Any]]:
        assert _conn is not None, "БД не подключена"
        cur = _conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        return [_row_to_dict(cur, r) for r in rows]
    return await _run(_op)


async def query_one(sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
    rows = await query(sql, params)
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# Мутации с аудитом (before/after) — основа «git для разума» и объяснимости
# --------------------------------------------------------------------------- #
async def _pk_col(table: str) -> str:
    """Определить имя PRIMARY KEY-столбца таблицы (напр. 'id' или 'key')."""
    info = await query(f"PRAGMA table_info({table})")
    for col in info:
        if col.get("pk"):
            return col["name"]
    return "id"


async def _snapshot(table: str, pk_col: str, row_id: str) -> Optional[dict[str, Any]]:
    return await query_one(f"SELECT * FROM {table} WHERE {pk_col} = ?", (row_id,))


async def mutate_with_audit(*, table: str, row_id: str, op: str,
                            sql: str, params: Sequence[Any],
                            pk_col: str = "id",
                            actor: str = "agent:orchestrator",
                            actor_kind: str = "agent",
                            reason: str = "") -> dict[str, Any]:
    """
    Выполнить мутацию и записать её в audit_changelog со снимками до/после.

    pk_col — имя первичного ключа таблицы (по умолчанию 'id'; для настроек 'key').
    Возвращает {ok, audit_id, before, after}. Любая правка (ручная из UI или
    авто из агента) проходит здесь → полная версионная история и возможность
    отката (rollback_change).
    """
    before = await _snapshot(table, pk_col, row_id) if op in ("update", "delete", "archive") else None
    await execute(sql, params)
    after = await _snapshot(table, pk_col, row_id) if op != "delete" else None

    audit_id = await insert_returning_id(
        "INSERT INTO audit_changelog "
        "(table_name, row_id, op, actor, actor_kind, before_json, after_json, reason) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (table, row_id, op, actor, actor_kind,
         json.dumps(before, ensure_ascii=False) if before else None,
         json.dumps(after, ensure_ascii=False) if after else None,
         reason))
    return {"ok": True, "audit_id": audit_id, "before": before, "after": after}


async def rollback_change(audit_id: int, actor: str = "local-admin") -> dict[str, Any]:
    """
    Откатить конкретную правку по записи аудита (восстановить before-снимок).
    Сам откат тоже логируется (op='rollback') — история неразрушима.
    """
    entry = await query_one("SELECT * FROM audit_changelog WHERE id = ?", (audit_id,))
    if not entry:
        return {"ok": False, "error": "Запись аудита не найдена."}
    table, row_id = entry["table_name"], entry["row_id"]
    pk = await _pk_col(table)
    before = json.loads(entry["before_json"]) if entry["before_json"] else None

    if before is None:
        # была операция create → откат = удаление
        await execute(f"DELETE FROM {table} WHERE {pk} = ?", (row_id,))
        restored = None
    else:
        cols = list(before.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != pk)
        await execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
            tuple(before[c] for c in cols))
        restored = before

    await insert_returning_id(
        "INSERT INTO audit_changelog (table_name,row_id,op,actor,actor_kind,"
        "before_json,after_json,reason) VALUES (?,?,?,?,?,?,?,?)",
        (table, row_id, "rollback", actor, "human",
         entry["after_json"], json.dumps(restored, ensure_ascii=False) if restored else None,
         f"rollback of audit #{audit_id}"))
    return {"ok": True, "restored": restored}


def new_id() -> str:
    """Короткий стабильный идентификатор строки."""
    return uuid.uuid4().hex
