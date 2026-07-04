# -*- coding: utf-8 -*-
"""
federation.py — федеративный обмен знаниями между инстансами JARVIS OS.

Идея (§10): инстансы делятся АНОНИМИЗИРОВАННЫМИ семантическими правилами
(visibility='federated'), не раскрывая приватные данные. Один узел экспортирует
пакет правил → другой импортирует, обогащая свой граф, помечая источник.

Гарантии приватности при экспорте:
    • Только узлы visibility='federated' и статус active/approved.
    • Вырезаются владелец (owner_id), source_trace, эмбеддинги (пересчитаются
      на импорте) и любые PII-подобные строки (пути, IP, e-mail, токены,
      хостнеймы, длинные hex) — очистка регуляркой.
    • Контент-хеш (sha256 нормализованного тела) — идентичность для дедупа на
      импорте (одно и то же правило из двух источников не дублируется).

Импорт:
    • Проверяет схему пакета и версию формата.
    • Дедуп по content_hash; новые правила заводятся как visibility='federated',
      status='draft', critic_verdict='pending' → проходят ЛОКАЛЬНЫЙ Critic перед
      активацией (чужие правила не доверяем слепо).
    • Помечает источник (federation_source) в tags и source_trace.

Формат пакета переносим (JSON): {format, version, exported_at, node, rules:[...]}.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Awaitable, Callable, Optional

from . import db, parallel

ChatFn = Callable[..., Awaitable[str]]

FORMAT = "jarvis-federation"
FORMAT_VERSION = 1
# Потолок одновременных Critic-ревью при импорте пачки правил (vLLM батчит).
IMPORT_CONCURRENCY_KEY = "JARVIS_FEDERATION_CONCURRENCY"
IMPORT_CONCURRENCY_DEFAULT = 4

# Регэкспы очистки PII/чувствительного при экспорте.
_PII = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"[\w.\-]+@[\w.\-]+\.\w+"), "<email>"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+|/(?:home|root|Users)/[^\s'\"]+"), "<path>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"(?i)(token|api[_-]?key|password|secret)\s*[:=]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"\bhttps?://[^\s'\"]+"), "<url>"),
]


def _anonymize(text: str) -> str:
    out = text or ""
    for rx, repl in _PII:
        out = rx.sub(repl, out)
    return out


def _content_hash(kind: str, title: str, body: str) -> str:
    norm = f"{kind}\n{title.strip().lower()}\n{body.strip().lower()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


async def export_rules(*, limit: int = 500, node: str = "jarvis") -> dict[str, Any]:
    """
    Собрать переносимый пакет анонимных federated-правил.
    """
    rows = await db.query(
        "SELECT kind, title, body, tags FROM semantic_knowledge_graph "
        "WHERE visibility='federated' AND status IN ('active') "
        "AND (critic_verdict IS NULL OR critic_verdict='approved') "
        "ORDER BY importance DESC LIMIT ?", (max(1, min(limit, 2000)),))
    rules = []
    seen: set[str] = set()
    for r in rows:
        title = _anonymize(r["title"])
        body = _anonymize(r["body"])
        chash = _content_hash(r["kind"], title, body)
        if chash in seen:
            continue
        seen.add(chash)
        try:
            tags = [t for t in json.loads(r["tags"] or "[]") if isinstance(t, str)]
        except (json.JSONDecodeError, TypeError):
            tags = []
        rules.append({"kind": r["kind"], "title": title, "body": body,
                      "tags": tags, "content_hash": chash})
    return {"format": FORMAT, "version": FORMAT_VERSION,
            "exported_at": time.time(), "node": node, "rules": rules}


async def import_rules(pack: dict[str, Any], *, source: str = "peer",
                       chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """
    Импортировать пакет правил. Дедуп по content_hash; новые заводятся как
    federated/draft/pending и прогоняются через ЛОКАЛЬНЫЙ Critic (чужому не
    доверяем слепо). Возврат: {imported, skipped, rejected}.
    """
    if not isinstance(pack, dict) or pack.get("format") != FORMAT:
        return {"ok": False, "error": "Неизвестный формат пакета федерации."}
    if int(pack.get("version", 0)) > FORMAT_VERSION:
        return {"ok": False, "error": "Версия формата новее поддерживаемой."}
    from . import subagents

    rules = pack.get("rules") or []
    imported = skipped = rejected = 0
    # существующие хеши (для дедупа) — считаем по локальному телу
    existing = await db.query(
        "SELECT kind, title, body FROM semantic_knowledge_graph WHERE visibility='federated'")
    have: set[str] = {_content_hash(e["kind"], e["title"], e["body"]) for e in existing}

    # 1) Дедуп/валидация (дёшево, последовательно) → список кандидатов.
    candidates: list[dict[str, str]] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind", "rule"))
        title = str(r.get("title", "")).strip()
        body = str(r.get("body", "")).strip()
        if not title or not body:
            continue
        chash = r.get("content_hash") or _content_hash(kind, title, body)
        if chash in have:
            skipped += 1
            continue
        have.add(chash)  # дедуп и внутри самой пачки
        tags = list(r.get("tags") or []) + [f"federation:{source}"]
        candidates.append({"kind": kind, "title": title, "body": body,
                           "tags": json.dumps(tags, ensure_ascii=False)})

    # 2) Локальный Critic-гейт КАЖДОГО кандидата — ПАРАЛЛЕЛЬНО (независимы, vLLM
    #    батчит). Чужим правилам не доверяем слепо: правила безопасности + опц. LLM.
    limit = parallel.concurrency(IMPORT_CONCURRENCY_KEY, IMPORT_CONCURRENCY_DEFAULT)
    reviews = await parallel.bounded_map(
        lambda c: subagents.critic_review(kind=c["kind"], title=c["title"],
                                          body=c["body"], chat=chat),
        candidates, limit=limit)

    # 3) Запись в граф (последовательно — один writer SQLite).
    for c, review in zip(candidates, reviews):
        status = "active" if review["verdict"] == "approved" else "rejected"
        if status == "rejected":
            rejected += 1
        else:
            imported += 1
        nid = db.new_id()
        await db.mutate_with_audit(
            table="semantic_knowledge_graph", row_id=nid, op="create", pk_col="id",
            sql="INSERT INTO semantic_knowledge_graph "
                "(id,kind,title,body,tags,visibility,status,critic_verdict,critic_notes,source_trace) "
                "VALUES (?,?,?,?,?, 'federated', ?,?,?,?)",
            params=(nid, c["kind"], c["title"], c["body"], c["tags"],
                    status, review["verdict"], review["notes"], f"federation:{source}"),
            actor="agent:federation", actor_kind="agent",
            reason=f"import from {source} ({review['verdict']})")
    return {"ok": True, "imported": imported, "skipped": skipped,
            "rejected": rejected, "source": source}
