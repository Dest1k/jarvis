# -*- coding: utf-8 -*-
"""
cc_bridge.py — безопасный мост оркестратора к «Когнитивному ядру».

Назначение: подтягивать RAG-контекст из загруженных файлов и вести
эпизодический лог (объяснимость) прямо из главного цикла агента — НЕ создавая
жёсткой зависимости. Если cognitive_core/БД недоступны (например, ядро
запущено без когнитивного слоя), все функции деградируют в no-op, а агент
работает как прежде.

Всё — best-effort: любое исключение проглатывается и логируется, поток чата
не прерывается.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

log = logging.getLogger("jarvis.cc_bridge")


def new_trace() -> str:
    """Уникальный id цепочки рассуждений (decision_trace) для объяснимости."""
    return uuid.uuid4().hex


def note_activity() -> None:
    """Отметить активность пользователя (для suspend/resume фонового цикла)."""
    try:
        from cognitive_core import suspend
        suspend.note_user_activity()
    except Exception:  # noqa: BLE001
        pass


async def rag_context(session_id: str, query: str, *, k: int = 4,
                      max_chars: int = 4000) -> tuple[str, list[str]]:
    """
    Вернуть (текст RAG-контекста, список file_id использованных чанков).
    Пусто, если нет загруженных файлов сессии / когнитивное ядро недоступно.
    """
    try:
        from cognitive_core import ingest
        hits = await ingest.search_chunks(query, session_id=session_id, k=k)
        # оставляем только уверенные совпадения (score>0)
        hits = [h for h in hits if h.get("score", 0) > 0.0]
        if not hits:
            return "", []
        parts, used, file_ids = [], 0, []
        for h in hits:
            block = f"[{h['file_name']}#{h['chunk_index']} score={h['score']}]\n{h['content']}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
            if h["file_id"] not in file_ids:
                file_ids.append(h["file_id"])
        return "\n\n".join(parts), file_ids
    except Exception as exc:  # noqa: BLE001
        log.debug("RAG-контекст недоступен: %s", exc)
        return "", []


async def log_episode(*, trace: str, session_id: str, entry_type: str, content: str,
                      outcome: Optional[str] = None,
                      used_file_ids: Optional[list[str]] = None,
                      step_index: Optional[int] = None) -> None:
    """Записать шаг в эпизодическую память (объяснимость). Best-effort."""
    try:
        import json
        from cognitive_core import db
        await db.execute(
            "INSERT INTO episodic_memory_logs "
            "(id,decision_trace,session_id,step_index,entry_type,content,"
            "used_file_ids,outcome) VALUES (?,?,?,?,?,?,?,?)",
            (db.new_id(), trace, session_id, step_index, entry_type,
             content[:4000], json.dumps(used_file_ids or []), outcome))
    except Exception as exc:  # noqa: BLE001
        log.debug("Эпизодический лог недоступен: %s", exc)


async def explain(session_id: str, *, limit: int = 12) -> dict[str, Any]:
    """
    Собрать трассу последнего хода для ответа на «почему ты так сделал?»:
    последние эпизоды + задействованные узлы графа и файлы.
    """
    try:
        import json
        from cognitive_core import db
        last = await db.query_one(
            "SELECT decision_trace FROM episodic_memory_logs WHERE session_id=? "
            "ORDER BY id DESC LIMIT 1", (session_id,))
        if not last:
            return {"trace": None, "steps": [], "files": [], "knowledge": []}
        trace = last["decision_trace"]
        steps = await db.query(
            "SELECT step_index,entry_type,content,used_knowledge_ids,used_file_ids,outcome "
            "FROM episodic_memory_logs WHERE decision_trace=? ORDER BY id LIMIT ?",
            (trace, limit))
        file_ids, know_ids = set(), set()
        for s in steps:
            for fid in json.loads(s.get("used_file_ids") or "[]"):
                file_ids.add(fid)
            for kid in json.loads(s.get("used_knowledge_ids") or "[]"):
                know_ids.add(kid)
        files = []
        for fid in file_ids:
            f = await db.query_one(
                "SELECT id,file_name FROM file_attachments_registry WHERE id=?", (fid,))
            if f:
                files.append(f)
        knowledge = []
        for kid in know_ids:
            k = await db.query_one(
                "SELECT id,title,kind FROM semantic_knowledge_graph WHERE id=?", (kid,))
            if k:
                knowledge.append(k)
        return {"trace": trace, "steps": steps, "files": files, "knowledge": knowledge}
    except Exception as exc:  # noqa: BLE001
        log.debug("explain недоступен: %s", exc)
        return {"trace": None, "steps": [], "files": [], "knowledge": []}
