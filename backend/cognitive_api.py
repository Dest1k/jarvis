# -*- coding: utf-8 -*-
"""
cognitive_api.py — REST/WebSocket-контракты «Когнитивного ядра» для дашборда.

Живая проводка backend/cognitive_core в FastAPI-ядро (server.py включает этот
router). Реализованы контракты из docs/cognitive_core_architecture.md §12:

    • Settings & Prompts Editor — hot-swap, DB перекрывает файл, reset-to-file.
    • Humanized DB Explorer     — CRUD над графом знаний + история версий + откат.
    • Unified DB Admin Browser  — чтение любой (whitelisted) таблицы + правка строк.
    • Audit / rollback          — «git для разума».
    • Cognitive state + WS      — Past / Present / Future.
    • Health snapshots          — self-healing лента.

КАЖДЫЙ мутирующий ответ несёт поля состояния для реактивного UI:
    {ok, state: loading|processing|success|error, data, audit_id, error}

Безопасность: имена таблиц/столбцов НЕЛЬЗЯ параметризовать в SQL, поэтому
DB-браузер работает ТОЛЬКО по белому списку таблиц/сортировок; значения —
всегда через биндинги. Порядок сортировки валидируется по фактическим столбцам.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from fastapi import APIRouter, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from cognitive_core import (config, db, ingest, learning, maintenance,
                            models, subagents)

router = APIRouter(prefix="/api/cognitive", tags=["cognitive-core"])

# Белый список таблиц для admin-браузера (без него — риск SQL-инъекции по имени).
_BROWSABLE_TABLES = {
    "agent_cognitive_state", "system_settings_and_prompts",
    "file_attachments_registry", "file_chunks", "semantic_knowledge_graph",
    "episodic_memory_logs", "agent_achievements", "audit_changelog",
    "system_health_snapshots", "user_roles_and_permissions",
    "project_plans", "project_tasks",
}
# Таблицы, редактируемые через admin-браузер (PUT), и их PK-столбец.
_EDITABLE_PK = {
    "system_settings_and_prompts": "key",
    "semantic_knowledge_graph": "id",
    "user_roles_and_permissions": "user_id",
    "project_tasks": "id",
    "agent_achievements": "id",
}


def _env(data: Any = None, *, ok: bool = True, state: str = "success",
         audit_id: Optional[int] = None, error: Optional[str] = None) -> JSONResponse:
    """Единый реактивный конверт ответа (см. §12)."""
    code = 200 if ok else 400
    return JSONResponse(
        {"ok": ok, "state": state if ok else "error",
         "data": data, "audit_id": audit_id, "error": error},
        status_code=code)


# --------------------------------------------------------------------------- #
# Settings & Prompts Editor (DB перекрывает файл)
# --------------------------------------------------------------------------- #
@router.get("/settings")
async def list_settings() -> JSONResponse:
    rows = await db.query(
        "SELECT key,value,value_type,category,description,is_active,file_default,"
        "version,updated_at FROM system_settings_and_prompts ORDER BY category,key")
    return _env({"settings": rows, "file_config": config._load_file()})


@router.post("/settings")
async def upsert_setting(payload: dict[str, Any]) -> JSONResponse:
    key = str(payload.get("key", "")).strip()
    if not key:
        return _env(ok=False, error="Не задан 'key'.")
    res = await config.set_setting(
        key, payload.get("value"),
        value_type=str(payload.get("value_type", "string")),
        category=str(payload.get("category", "general")),
        description=str(payload.get("description", "")),
        actor=str(payload.get("actor", "local-admin")),
        reason=str(payload.get("reason", "UI edit")))
    return _env({"key": key, "effective": await config.get_setting(key)},
                audit_id=res.get("audit_id"))


@router.post("/settings/reset")
async def reset_setting(payload: dict[str, Any]) -> JSONResponse:
    key = str(payload.get("key", "")).strip()
    if not key:
        return _env(ok=False, error="Не задан 'key'.")
    res = await config.reset_to_file_default(key, actor=str(payload.get("actor", "local-admin")))
    return _env({"key": key, "effective": await config.get_setting(key)},
                audit_id=res.get("audit_id"))


# --------------------------------------------------------------------------- #
# Humanized DB Explorer — граф знаний (CRUD + версии + откат)
# --------------------------------------------------------------------------- #
@router.get("/graph")
async def list_graph(kind: str = "", status: str = "", q: str = "",
                     limit: int = 100, offset: int = 0) -> JSONResponse:
    where, params = [], []
    if kind:
        where.append("kind = ?"); params.append(kind)
    if status:
        where.append("status = ?"); params.append(status)
    if q:
        where.append("(title LIKE ? OR body LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params += [max(1, min(limit, 500)), max(0, offset)]
    rows = await db.query(
        f"SELECT * FROM semantic_knowledge_graph {clause} "
        f"ORDER BY importance DESC, updated_at DESC LIMIT ? OFFSET ?", params)
    return _env({"nodes": rows, "count": len(rows)})


@router.get("/graph/{node_id}")
async def get_graph_node(node_id: str) -> JSONResponse:
    node = await db.query_one("SELECT * FROM semantic_knowledge_graph WHERE id = ?", (node_id,))
    if node is None:
        return _env(ok=False, error="Узел не найден.")
    history = await db.query(
        "SELECT id,op,actor,actor_kind,reason,created_at FROM audit_changelog "
        "WHERE table_name='semantic_knowledge_graph' AND row_id=? ORDER BY id DESC", (node_id,))
    return _env({"node": node, "versions": history})


@router.post("/graph")
async def create_graph_node(payload: dict[str, Any]) -> JSONResponse:
    nid = db.new_id()
    res = await db.mutate_with_audit(
        table="semantic_knowledge_graph", row_id=nid, op="create", pk_col="id",
        sql="INSERT INTO semantic_knowledge_graph "
            "(id,kind,title,body,tags,owner_id,visibility,importance,status,critic_verdict,source_trace) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        params=(nid, str(payload.get("kind", "rule")), str(payload.get("title", "")),
                str(payload.get("body", "")),
                json.dumps(payload.get("tags", []), ensure_ascii=False),
                payload.get("owner_id", "local-admin"),
                str(payload.get("visibility", "team")),
                float(payload.get("importance", 0.5)),
                str(payload.get("status", "draft")),
                payload.get("critic_verdict"), payload.get("source_trace")),
        actor=str(payload.get("actor", "local-admin")), actor_kind="human",
        reason=str(payload.get("reason", "manual create")))
    return _env({"id": nid, "node": res.get("after")}, audit_id=res.get("audit_id"))


@router.put("/graph/{node_id}")
async def update_graph_node(node_id: str, payload: dict[str, Any]) -> JSONResponse:
    allowed = {"kind", "title", "body", "tags", "visibility", "importance",
               "status", "critic_verdict", "critic_notes"}
    fields, params = [], []
    for k, v in payload.items():
        if k not in allowed:
            continue
        fields.append(f"{k} = ?")
        params.append(json.dumps(v, ensure_ascii=False) if k == "tags" else v)
    if not fields:
        return _env(ok=False, error="Нет допустимых полей для обновления.")
    fields.append("version = version + 1")
    fields.append("updated_at = unixepoch('subsec')")
    params.append(node_id)
    res = await db.mutate_with_audit(
        table="semantic_knowledge_graph", row_id=node_id, op="update", pk_col="id",
        sql=f"UPDATE semantic_knowledge_graph SET {', '.join(fields)} WHERE id = ?",
        params=params, actor=str(payload.get("actor", "local-admin")),
        actor_kind="human", reason=str(payload.get("reason", "manual update")))
    return _env({"node": res.get("after")}, audit_id=res.get("audit_id"))


@router.delete("/graph/{node_id}")
async def delete_graph_node(node_id: str, archive: bool = True,
                            actor: str = "local-admin") -> JSONResponse:
    # По умолчанию АРХИВИРУЕМ (не удаляем) — memory-forgetting: можно «вспомнить».
    if archive:
        res = await db.mutate_with_audit(
            table="semantic_knowledge_graph", row_id=node_id, op="archive", pk_col="id",
            sql="UPDATE semantic_knowledge_graph SET status='archived', "
                "updated_at=unixepoch('subsec') WHERE id=?",
            params=(node_id,), actor=actor, actor_kind="human", reason="archive")
        return _env({"archived": node_id}, audit_id=res.get("audit_id"))
    res = await db.mutate_with_audit(
        table="semantic_knowledge_graph", row_id=node_id, op="delete", pk_col="id",
        sql="DELETE FROM semantic_knowledge_graph WHERE id=?",
        params=(node_id,), actor=actor, actor_kind="human", reason="delete")
    return _env({"deleted": node_id}, audit_id=res.get("audit_id"))


# --------------------------------------------------------------------------- #
# Audit / rollback («git для разума»)
# --------------------------------------------------------------------------- #
@router.get("/audit")
async def list_audit(table: str = "", row_id: str = "", limit: int = 100) -> JSONResponse:
    where, params = [], []
    if table:
        where.append("table_name = ?"); params.append(table)
    if row_id:
        where.append("row_id = ?"); params.append(row_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(limit, 500)))
    rows = await db.query(
        f"SELECT * FROM audit_changelog {clause} ORDER BY id DESC LIMIT ?", params)
    return _env({"changelog": rows})


@router.post("/audit/rollback")
async def rollback(payload: dict[str, Any]) -> JSONResponse:
    audit_id = payload.get("audit_id")
    if audit_id is None:
        return _env(ok=False, error="Не задан 'audit_id'.")
    res = await db.rollback_change(int(audit_id), actor=str(payload.get("actor", "local-admin")))
    if not res.get("ok"):
        return _env(ok=False, error=res.get("error", "Откат не удался."))
    return _env({"restored": res.get("restored")})


# --------------------------------------------------------------------------- #
# Unified DB Admin Browser (whitelisted tables)
# --------------------------------------------------------------------------- #
@router.get("/db/tables")
async def list_tables() -> JSONResponse:
    out = []
    for t in sorted(_BROWSABLE_TABLES):
        cnt = await db.query_one(f"SELECT COUNT(*) AS n FROM {t}")
        out.append({"table": t, "rows": (cnt or {}).get("n", 0),
                    "editable": t in _EDITABLE_PK})
    return _env({"tables": out})


@router.get("/db/{table}")
async def browse_table(table: str, q: str = "", limit: int = 50,
                       offset: int = 0) -> JSONResponse:
    if table not in _BROWSABLE_TABLES:
        return _env(ok=False, error="Таблица недоступна для просмотра.")
    cols = [c["name"] for c in await db.query(f"PRAGMA table_info({table})")]
    where, params = "", []
    if q:
        # поиск по текстовым столбцам (LIKE) — имена столбцов из PRAGMA (безопасны)
        text_cols = [c for c in cols]
        where = "WHERE " + " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in text_cols)
        params = [f"%{q}%"] * len(text_cols)
    params += [max(1, min(limit, 200)), max(0, offset)]
    rows = await db.query(f"SELECT * FROM {table} {where} LIMIT ? OFFSET ?", params)
    total = await db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
    return _env({"table": table, "columns": cols, "rows": rows,
                 "total": (total or {}).get("n", 0)})


@router.put("/db/{table}/{row_id}")
async def edit_row(table: str, row_id: str, payload: dict[str, Any]) -> JSONResponse:
    pk = _EDITABLE_PK.get(table)
    if pk is None:
        return _env(ok=False, error="Таблица не редактируется через браузер.")
    cols = {c["name"] for c in await db.query(f"PRAGMA table_info({table})")}
    updates = {k: v for k, v in (payload.get("values") or {}).items()
               if k in cols and k != pk}
    if not updates:
        return _env(ok=False, error="Нет допустимых полей.")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [row_id]
    res = await db.mutate_with_audit(
        table=table, row_id=row_id, op="update", pk_col=pk,
        sql=f"UPDATE {table} SET {set_clause} WHERE {pk} = ?", params=params,
        actor=str(payload.get("actor", "local-admin")), actor_kind="human",
        reason=str(payload.get("reason", "admin browser edit")))
    return _env({"row": res.get("after")}, audit_id=res.get("audit_id"))


# --------------------------------------------------------------------------- #
# Cognitive state + Health
# --------------------------------------------------------------------------- #
async def _cognition_snapshot() -> dict[str, Any]:
    state = await db.query_one("SELECT * FROM agent_cognitive_state WHERE id = 1") or {}
    succ = await db.query_one(
        "SELECT COUNT(*) AS n FROM episodic_memory_logs WHERE outcome='success'")
    fail = await db.query_one(
        "SELECT COUNT(*) AS n FROM episodic_memory_logs WHERE outcome='failure'")
    present = await db.query(
        "SELECT content,entry_type,created_at FROM episodic_memory_logs "
        "ORDER BY id DESC LIMIT 1")
    future = await db.query(
        "SELECT id,title,status,progress FROM project_tasks "
        "WHERE status IN ('todo','in_progress') ORDER BY order_index LIMIT 10")
    return {
        "past": {"successes": (succ or {}).get("n", 0), "failures": (fail or {}).get("n", 0)},
        "present": {"state": state.get("state", "IDLE"),
                    "active_goal": state.get("active_goal"),
                    "last": present[0] if present else None},
        "future": {"learning_queue": future},
    }


@router.get("/state")
async def cognitive_state() -> JSONResponse:
    return _env(await _cognition_snapshot())


@router.get("/explain")
async def explain(session_id: str = "default") -> JSONResponse:
    """«Почему ты так сделал?» — трасса последнего хода: шаги + файлы + знания."""
    try:
        from orchestrator import cc_bridge
        return _env(await cc_bridge.explain(session_id))
    except Exception as exc:  # noqa: BLE001
        return _env(ok=False, error=f"explain недоступен: {exc}")


@router.get("/health")
async def health_snapshots(limit: int = 30) -> JSONResponse:
    # последние снимки по каждому компоненту
    rows = await db.query(
        "SELECT * FROM system_health_snapshots ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 200)),))
    return _env({"snapshots": rows})


@router.post("/health/record")
async def record_health(payload: dict[str, Any]) -> JSONResponse:
    snap = models.HealthSnapshot(**payload)
    await db.execute(
        "INSERT INTO system_health_snapshots (component,status,detail,suggested_fix,fix_applied) "
        "VALUES (?,?,?,?,?)",
        (snap.component, snap.status, snap.detail, snap.suggested_fix, int(snap.fix_applied)))
    return _env({"recorded": snap.component})


# --------------------------------------------------------------------------- #
# RAG: загрузка файлов и семантический поиск (Core Chat / вложения)
# --------------------------------------------------------------------------- #
@router.post("/files/upload")
async def upload_file(file: UploadFile = File(...),
                      session_id: str = Form("default"),
                      message_id: str = Form(""),
                      owner_id: str = Form("local-admin")) -> JSONResponse:
    """
    Приём файла из чата: dedup(sha256) → parse → chunk → embed → ready.
    Немедленно возвращает запись реестра с итоговым ingest_status и числом чанков
    (готовых к RAG-инъекции в текущую сессию).
    """
    data = await file.read()
    if not data:
        return _env(ok=False, error="Пустой файл.")
    try:
        rec = await ingest.ingest_file(
            filename=file.filename or "upload.bin", data=data,
            owner_id=owner_id, session_id=session_id or "default",
            message_id=message_id or None, origin="user")
    except Exception as exc:  # noqa: BLE001
        return _env(ok=False, error=f"Сбой конвейера ingestion: {exc}")
    ok = rec.get("ingest_status") == "ready"
    return _env({"file": rec, "deduplicated": rec.get("deduplicated", False)},
                ok=True, state="success" if ok else "processing")


@router.get("/files")
async def list_files(session_id: str = "", limit: int = 100) -> JSONResponse:
    where, params = [], []
    if session_id:
        where.append("session_id = ?"); params.append(session_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(limit, 500)))
    rows = await db.query(
        f"SELECT id,file_name,mime_type,size_bytes,ingest_status,ingest_error,"
        f"token_count,chunk_count,session_id,origin,uploaded_at "
        f"FROM file_attachments_registry {clause} ORDER BY uploaded_at DESC LIMIT ?", params)
    return _env({"files": rows})


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, actor: str = "local-admin") -> JSONResponse:
    res = await db.mutate_with_audit(
        table="file_attachments_registry", row_id=file_id, op="delete", pk_col="id",
        sql="DELETE FROM file_attachments_registry WHERE id=?", params=(file_id,),
        actor=actor, actor_kind="human", reason="delete file")
    # чанки удалятся каскадом (ON DELETE CASCADE)
    return _env({"deleted": file_id}, audit_id=res.get("audit_id"))


@router.get("/rag/status")
async def rag_status() -> JSONResponse:
    """Активный провайдер эмбеддингов и векторный бэкенд поиска."""
    import os as _os
    return _env({
        "embed_provider": _os.environ.get("JARVIS_EMBED_PROVIDER", "local"),
        "vector_backend": ingest.vector_backend(),
        "sqlite_vec": ingest.sqlite_vec_available(),
        "embed_dim": ingest.EMBED_DIM,
    })


@router.post("/rag/reembed")
async def rag_reembed() -> JSONResponse:
    """Пересчитать эмбеддинги всех чанков активным провайдером (после смены)."""
    return _env(await ingest.reembed_all())


@router.post("/rag/search")
async def rag_search(payload: dict[str, Any]) -> JSONResponse:
    query = str(payload.get("query", "")).strip()
    if not query:
        return _env(ok=False, error="Не задан 'query'.")
    hits = await ingest.search_chunks(
        query, session_id=payload.get("session_id") or None,
        k=int(payload.get("k", 5)))
    return _env({"query": query, "hits": hits})


# --------------------------------------------------------------------------- #
# Суб-агенты: Critic-гейт и декомпозиция целей → планы
# --------------------------------------------------------------------------- #
def _dispatcher_chat():
    """Инъектируемый chat к диспетчер-модели (или None, если недоступна)."""
    try:
        from orchestrator import llm
        return llm.chat
    except Exception:  # noqa: BLE001
        return None


@router.post("/critic/review")
async def critic_review(payload: dict[str, Any]) -> JSONResponse:
    """Прогнать текст навыка/правила через Critic (правила + опц. LLM)."""
    review = await subagents.critic_review(
        kind=str(payload.get("kind", "rule")),
        title=str(payload.get("title", "")),
        body=str(payload.get("body", "")),
        chat=_dispatcher_chat() if payload.get("use_llm") else None)
    return _env(review, ok=True,
                state="success" if review["verdict"] == "approved" else "processing")


@router.post("/knowledge/commit")
async def knowledge_commit(payload: dict[str, Any]) -> JSONResponse:
    """Critic-гейт → коммит узла в граф (active) или сохранение rejected."""
    res = await subagents.commit_knowledge_if_approved(
        kind=str(payload.get("kind", "rule")),
        title=str(payload.get("title", "")),
        body=str(payload.get("body", "")),
        tags=payload.get("tags") or [],
        owner_id=str(payload.get("owner_id", "local-admin")),
        source_trace=str(payload.get("source_trace", "")),
        chat=_dispatcher_chat() if payload.get("use_llm") else None)
    return _env(res, ok=True,
                state="success" if res["status"] == "active" else "processing")


@router.post("/plans/decompose")
async def plans_decompose(payload: dict[str, Any]) -> JSONResponse:
    """Декомпозировать цель в план с задачами и зависимостями."""
    goal = str(payload.get("goal", "")).strip()
    if not goal:
        return _env(ok=False, error="Не задана 'goal'.")
    res = await subagents.decompose_goal(
        goal, owner_id=str(payload.get("owner_id", "local-admin")),
        chat=_dispatcher_chat() if payload.get("use_llm") else None)
    return _env(res)


async def _sandbox_runner(code: str, lang: str):
    """Best-effort исполнение кода в sandbox-контейнере (или деградация)."""
    try:
        from orchestrator.tools import tool_run_code, ToolContext
        res = await tool_run_code({"language": lang, "code": code}, ToolContext())
        return (0 if res.get("ok") else 1), res.get("content", "")
    except Exception as exc:  # noqa: BLE001
        return None, f"sandbox недоступен: {exc}"


@router.post("/plans/{plan_id}/run")
async def run_plan(plan_id: str, payload: Optional[dict[str, Any]] = None) -> JSONResponse:
    """Автономно прогнать план суб-агентами (Researcher→Coder/sandbox→Critic)."""
    from cognitive_core import executor
    payload = payload or {}
    chat = _dispatcher_chat()
    sandbox = _sandbox_runner if payload.get("execute", True) else None
    report = await executor.run_plan(
        plan_id, chat=chat, sandbox_run=sandbox,
        autonomy_level=int(payload.get("autonomy_level", 2)),
        session_id=str(payload.get("session_id", "executor")))
    if not report.get("ok"):
        return _env(ok=False, error=report.get("error", "Прогон не удался."))
    return _env(report, state="success" if report["status"] == "done" else "processing")


@router.get("/plans")
async def list_plans(limit: int = 50) -> JSONResponse:
    rows = await db.query(
        "SELECT * FROM project_plans ORDER BY created_at DESC LIMIT ?",
        (max(1, min(limit, 200)),))
    return _env({"plans": rows})


@router.get("/plans/{plan_id}/tasks")
async def plan_tasks(plan_id: str) -> JSONResponse:
    rows = await db.query(
        "SELECT * FROM project_tasks WHERE plan_id=? ORDER BY order_index", (plan_id,))
    return _env({"plan_id": plan_id, "tasks": rows})


@router.put("/plans/{plan_id}/tasks/{task_id}")
async def update_plan_task(plan_id: str, task_id: str, payload: dict[str, Any]) -> JSONResponse:
    allowed = {"title", "assignee", "status", "progress", "result", "order_index"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if not fields:
        return _env(ok=False, error="Нет допустимых полей.")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [task_id, plan_id]
    res = await db.mutate_with_audit(
        table="project_tasks", row_id=task_id, op="update", pk_col="id",
        sql=f"UPDATE project_tasks SET {set_clause} WHERE id=? AND plan_id=?",
        params=params, actor=str(payload.get("actor", "local-admin")),
        actor_kind="human", reason="manual task edit")
    return _env({"task": res.get("after")}, audit_id=res.get("audit_id"))


# --------------------------------------------------------------------------- #
# Sleep-Cycle / память: консолидация, forgetting, recall, post-mortem
# --------------------------------------------------------------------------- #
@router.post("/maintenance/sleep-cycle")
async def run_sleep_cycle() -> JSONResponse:
    """Прогнать sleep-cycle: decay → prune → consolidate (для idle/ручного запуска)."""
    return _env(await maintenance.sleep_cycle())


@router.post("/maintenance/recall/{node_id}")
async def recall_node(node_id: str) -> JSONResponse:
    return _env(await maintenance.recall(node_id))


@router.post("/maintenance/reinforce/{node_id}")
async def reinforce_node(node_id: str) -> JSONResponse:
    return _env(await maintenance.reinforce(node_id))


@router.post("/maintenance/post-mortem")
async def post_mortem(payload: dict[str, Any]) -> JSONResponse:
    """Разбор завершённой задачи: success/failure → эпизод + корректирующее знание."""
    res = await maintenance.post_mortem(
        session_id=str(payload.get("session_id", "default")),
        task=str(payload.get("task", "")),
        outcome=str(payload.get("outcome", "unknown")),
        detail=str(payload.get("detail", "")),
        chat=_dispatcher_chat() if payload.get("use_llm") else None)
    return _env(res)


# --------------------------------------------------------------------------- #
# Lifelong Learning: фоновый цикл самосовершенствования (suspend/resume)
# --------------------------------------------------------------------------- #
@router.get("/learning/status")
async def learning_status() -> JSONResponse:
    return _env(await learning.status())


@router.post("/learning/start")
async def learning_start() -> JSONResponse:
    await learning.start()
    return _env(await learning.status())


@router.post("/learning/suspend")
async def learning_suspend(payload: dict[str, Any]) -> JSONResponse:
    await learning.on_user_activity(
        active_user=str(payload.get("user", "local-admin")),
        goal=str(payload.get("goal", "")))
    return _env(await learning.status())


@router.post("/learning/iterate")
async def learning_iterate(payload: dict[str, Any]) -> JSONResponse:
    """Ручной прогон ОДНОЙ итерации обучения (для демонстрации/теста)."""
    res = await learning.learning_iteration(
        int(payload.get("iteration", 0)),
        chat=_dispatcher_chat() if payload.get("use_llm") else None)
    return _env(res)


@router.websocket("/stream")
async def cognition_stream(ws: WebSocket) -> None:
    """Live Cognitive Stream: Past / Present / Future (пуш раз в ~2 с)."""
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(
                {"type": "cognition", "ts": time.time(), **(await _cognition_snapshot())},
                ensure_ascii=False))
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        return
