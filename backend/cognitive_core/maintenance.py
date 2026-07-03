# -*- coding: utf-8 -*-
"""
maintenance.py — Sleep-Cycle Consolidation и приоритизация памяти (forgetting).

Фоновые рутины в простое системы (§14):
    • apply_decay      — экспоненциальный распад decay_score узлов графа по
                         времени без использования; редкие/устаревшие уходят в
                         АРХИВ (status='archived', НЕ удаляются) → чистое
                         активное окно. use_count/last_used подтягивают обратно.
    • recall           — «вспомнить» архивный узел, если снова релевантен.
    • reinforce        — усилить узел при использовании (use_count↑, decay=1,
                         importance подрастает) — противовес распаду.
    • prune_junk_logs  — удалить малоценные эпизодические логи (thought/observation)
                         старше порога, сохранив успехи/провалы/ошибки.
    • consolidate      — свернуть точные дубли активных знаний в один узел.
    • post_mortem      — классифицировать сессию success/failure, при провале —
                         сгенерировать корректирующее знание (через Critic-гейт).
    • sleep_cycle      — единый прогон: decay → prune → consolidate + отчёт.

Все операции — через offloaded db-слой (event loop свободен) и идемпотентны.
Пороги — из system_settings_and_prompts (DB override), с файловыми дефолтами.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from . import config, db

ChatFn = Callable[..., Awaitable[str]]

# Дефолты (перекрываются ключами в system_settings_and_prompts).
_DEFAULTS = {
    "maint.decay_half_life_days": 14.0,     # период полураспада decay_score
    "maint.archive_threshold": 0.15,        # ниже — в архив
    "maint.junk_log_age_days": 7.0,         # старше — прунить thought/observation
    "maint.min_importance_keep": 0.4,       # важные не архивируются
}


async def _th(key: str) -> float:
    return float(await config.get_setting(key, _DEFAULTS[key]))


# --------------------------------------------------------------------------- #
# Распад и архивирование (forgetting) / усиление (reinforce)
# --------------------------------------------------------------------------- #
async def apply_decay() -> dict[str, Any]:
    """
    Пересчитать decay_score активных узлов по времени с последнего использования
    и заархивировать те, кто упал ниже порога (кроме важных).
    Возврат: {updated, archived}.
    """
    half_life = max(0.5, await _th("maint.decay_half_life_days"))
    threshold = await _th("maint.archive_threshold")
    keep_imp = await _th("maint.min_importance_keep")
    now = time.time()
    rows = await db.query(
        "SELECT id, importance, last_used_at, created_at FROM semantic_knowledge_graph "
        "WHERE status='active'")
    updated = archived = 0
    for r in rows:
        ref = r["last_used_at"] or r["created_at"] or now
        age_days = max(0.0, (now - ref) / 86400.0)
        new_decay = 0.5 ** (age_days / half_life)   # 1.0 свежий → 0.5 за half_life
        await db.execute(
            "UPDATE semantic_knowledge_graph SET decay_score=? WHERE id=?",
            (round(new_decay, 4), r["id"]))
        updated += 1
        if new_decay < threshold and (r["importance"] or 0) < keep_imp:
            await db.mutate_with_audit(
                table="semantic_knowledge_graph", row_id=r["id"], op="archive", pk_col="id",
                sql="UPDATE semantic_knowledge_graph SET status='archived', "
                    "updated_at=unixepoch('subsec') WHERE id=?",
                params=(r["id"],), actor="agent:sleep-cycle", actor_kind="agent",
                reason=f"decay {new_decay:.3f} < {threshold} (forgetting)")
            archived += 1
    return {"updated": updated, "archived": archived}


async def reinforce(node_id: str) -> dict[str, Any]:
    """Усилить узел при использовании: use_count↑, decay=1.0, importance подрастает."""
    await db.execute(
        "UPDATE semantic_knowledge_graph SET use_count=use_count+1, decay_score=1.0, "
        "last_used_at=unixepoch('subsec'), "
        "importance=MIN(1.0, importance+0.05) WHERE id=?", (node_id,))
    return {"reinforced": node_id}


async def recall(node_id: str) -> dict[str, Any]:
    """«Вспомнить» архивный узел — вернуть в active и усилить."""
    res = await db.mutate_with_audit(
        table="semantic_knowledge_graph", row_id=node_id, op="update", pk_col="id",
        sql="UPDATE semantic_knowledge_graph SET status='active', decay_score=1.0, "
            "last_used_at=unixepoch('subsec') WHERE id=?",
        params=(node_id,), actor="agent:recall", actor_kind="agent", reason="recall from archive")
    return {"recalled": node_id, "audit_id": res.get("audit_id")}


# --------------------------------------------------------------------------- #
# Прунинг мусорных логов и консолидация дублей
# --------------------------------------------------------------------------- #
async def prune_junk_logs() -> dict[str, Any]:
    """
    Удалить малоценные эпизодические логи (thought/observation) старше порога.
    Успехи/провалы/ошибки/гипотезы СОХРАНЯЮТСЯ (ценный опыт).
    """
    age_days = await _th("maint.junk_log_age_days")
    cutoff = time.time() - age_days * 86400.0
    n = await db.execute(
        "DELETE FROM episodic_memory_logs WHERE entry_type IN ('thought','observation') "
        "AND (outcome IS NULL OR outcome='unknown') AND created_at < ?", (cutoff,))
    return {"pruned": n}


async def consolidate() -> dict[str, Any]:
    """
    Свернуть ТОЧНЫЕ дубли активных знаний (одинаковый kind+body) в один узел
    (оставляем самый используемый), прочие — в архив. Простой безопасный шаг
    консолидации; семантическое слияние — при наличии эмбеддинг-поиска.
    """
    dupes = await db.query(
        "SELECT kind, body, COUNT(*) AS n FROM semantic_knowledge_graph "
        "WHERE status='active' GROUP BY kind, body HAVING n > 1")
    merged = 0
    for d in dupes:
        rows = await db.query(
            "SELECT id, use_count FROM semantic_knowledge_graph "
            "WHERE status='active' AND kind=? AND body=? ORDER BY use_count DESC",
            (d["kind"], d["body"]))
        for r in rows[1:]:   # оставляем первый (самый используемый)
            await db.execute(
                "UPDATE semantic_knowledge_graph SET status='archived', "
                "updated_at=unixepoch('subsec') WHERE id=?", (r["id"],))
            merged += 1
    return {"merged_duplicates": merged}


# --------------------------------------------------------------------------- #
# Post-Task Post-Mortem (Experience Harvesting)
# --------------------------------------------------------------------------- #
async def post_mortem(*, session_id: str, task: str, outcome: str,
                      detail: str = "", chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """
    Классифицировать завершённую задачу и извлечь опыт:
      success → эпизод success (усиление паттернов),
      failure → эпизод failure + root-cause + корректирующее знание (Critic-гейт).
    """
    trace = db.new_id()
    await db.execute(
        "INSERT INTO episodic_memory_logs (id,decision_trace,session_id,entry_type,content,outcome) "
        "VALUES (?,?,?,?,?,?)",
        (db.new_id(), trace, session_id,
         "success" if outcome == "success" else "failure",
         f"POST-MORTEM [{outcome}] {task}\n{detail}", outcome))

    corrective = None
    if outcome == "failure":
        # сгенерировать корректирующее правило (через Critic-гейт)
        from . import subagents
        body = detail or f"Задача провалилась: {task}. Проанализировать причину и не повторять."
        title = f"Корректирующее правило: {task[:60]}"
        if chat is not None:
            try:
                raw = await chat([
                    {"role": "system", "content":
                     "Ты — аналитик root-cause JARVIS. По провалу сформулируй ОДНО "
                     "краткое корректирующее правило системного администрирования "
                     "(что делать в следующий раз). Только текст правила."},
                    {"role": "user", "content": f"Задача: {task}\nДетали провала: {detail}"}],
                    temperature=0.2, max_tokens=300, timeout=60)
                if raw.strip():
                    body = raw.strip()
            except Exception:  # noqa: BLE001
                pass
        corrective = await subagents.commit_knowledge_if_approved(
            kind="rule", title=title, body=body, tags=["corrective", "post-mortem"],
            source_trace=trace, chat=chat)

    await db.execute(
        "INSERT INTO agent_achievements (id,title,detail,category,importance) VALUES (?,?,?,?,?)",
        (db.new_id(),
         f"{'✅ Успех' if outcome=='success' else '🛠 Разбор провала'}: {task[:80]}",
         detail[:400], "milestone" if outcome == "success" else "recovery",
         0.6 if outcome == "success" else 0.5))
    return {"trace": trace, "outcome": outcome, "corrective": corrective}


# --------------------------------------------------------------------------- #
# Единый прогон sleep-cycle
# --------------------------------------------------------------------------- #
async def sleep_cycle() -> dict[str, Any]:
    """Полный цикл консолидации в простое: decay → prune → consolidate."""
    started = time.time()
    decay = await apply_decay()
    prune = await prune_junk_logs()
    cons = await consolidate()
    report = {"decay": decay, "prune": prune, "consolidate": cons,
              "took_sec": round(time.time() - started, 3)}
    await db.execute(
        "INSERT INTO system_health_snapshots (component,status,detail) VALUES (?,?,?)",
        ("sleep-cycle", "healthy",
         f"archived={decay['archived']} pruned={prune['pruned']} "
         f"merged={cons['merged_duplicates']}"))
    return report
