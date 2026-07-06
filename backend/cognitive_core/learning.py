# -*- coding: utf-8 -*-
"""
learning.py — фоновый цикл «Lifelong Learning» JARVIS OS.

Экзистенциальная директива (§0): «Чего мне ещё не хватает, чтобы стать
идеальным системным администратором и ассистентом?» На простое системы воркер
делает итерации самосовершенствования, ЯКОРЯ их вокруг высокоуровневого
системного администрирования, автоматизации и ассистент-паттернов.

Одна итерация теперь обучается из двух источников:
    1. resolved_incidents.json → проверенные рецепты ошибок превращаются в знания;
    2. sysadmin topic rotation → Researcher/Critic генерируют новые правила.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

from . import db, subagents, suspend

log = logging.getLogger("jarvis.learning")

ChatFn = Callable[..., Awaitable[str]]
ENABLED = os.environ.get("JARVIS_LIFELONG_LEARNING", "0") == "1"
ITER_PAUSE_SEC = float(os.environ.get("JARVIS_LEARNING_PAUSE", "30"))
MINE_INCIDENTS = os.environ.get("JARVIS_LEARNING_MINE_INCIDENTS", "1") != "0"

_TOPICS = [
    "мониторинг и алертинг (Prometheus/Grafana/Alertmanager)",
    "автоматизация конфигураций (Ansible/idempotent playbooks)",
    "контейнеризация и оркестрация (Docker/Compose/systemd)",
    "безопасность хоста (firewall, least-privilege, аудит)",
    "резервное копирование и восстановление (снапшоты, проверка бэкапов)",
    "производительность и диагностика (perf, iostat, journald)",
    "сетевые сервисы (nginx/haproxy, TLS, DNS)",
    "надёжность и наблюдаемость (SLO, логи, трейсинг)",
    "инкремент-обновления и откаты (blue-green, canary)",
    "ассистент-паттерны (краткость, подтверждение опасного, объяснимость)",
]

_BASELINE_RULES = {
    "мониторинг и алертинг (Prometheus/Grafana/Alertmanager)":
        "Каждый критичный сервис должен экспортировать метрики и иметь алерт на доступность (up==0) и насыщение ресурса (CPU/RAM/диск >85%).",
    "автоматизация конфигураций (Ansible/idempotent playbooks)":
        "Все изменения конфигурации выполнять идемпотентными плейбуками с --check/--diff перед применением; ручные правки на проде запрещены.",
    "безопасность хоста (firewall, least-privilege, аудит)":
        "По умолчанию deny на входящий трафик; открывать только нужные порты; sudo — только именованным операторам с логированием команд.",
    "резервное копирование и восстановление (снапшоты, проверка бэкапов)":
        "Бэкап без проверенного восстановления не считается бэкапом — регулярно прогонять restore-тест в изолированной среде.",
}


def _pick_topic(iteration: int) -> str:
    return _TOPICS[iteration % len(_TOPICS)]


async def _already_has_incident_rule(incident_id: str) -> bool:
    row = await db.query_one(
        "SELECT id FROM semantic_knowledge_graph WHERE kind='incident_recipe' AND source_trace=? LIMIT 1",
        (incident_id,),
    )
    return row is not None


async def _mine_incident(iteration: int, *, chat: Optional[ChatFn] = None) -> Optional[dict[str, Any]]:
    """Превратить проверенный resolved incident в знание cognitive graph."""
    if not MINE_INCIDENTS:
        return None
    try:
        from orchestrator.incidents import IncidentLedger
        items = IncidentLedger().all(limit=50)
    except Exception as exc:  # noqa: BLE001
        log.debug("incident mining unavailable: %s", exc)
        return None
    if not items:
        return None
    # Идём по ротации, чтобы не долбить один и тот же свежий incident.
    for offset in range(min(len(items), 12)):
        inc = items[(iteration + offset) % len(items)]
        inc_id = str(inc.get("id") or inc.get("signature") or "")
        if not inc_id or await _already_has_incident_rule(inc_id):
            continue
        signature = str(inc.get("signature", ""))[:160]
        error = str(inc.get("error", ""))[:700]
        resolution = str(inc.get("resolution", ""))[:1200]
        if not resolution.strip():
            continue
        body = (
            f"Когда встречается ошибка с сигнатурой `{signature}`, сначала проверь уже известный рецепт:\n"
            f"1. Симптом/ошибка: {error}\n"
            f"2. Проверенное решение: {resolution}\n"
            "3. Перед применением убедись, что контекст совпадает; если нет — используй как гипотезу, а не как приказ."
        )
        if chat is not None:
            try:
                refined = (await subagents.run_role(
                    "coder",
                    "Сожми этот resolved incident в безопасный, проверяемый runbook-рецепт без опасных команд по умолчанию:\n" + body,
                    chat=chat,
                )).get("content")
                if refined:
                    body = refined[:1600]
            except Exception:  # noqa: BLE001
                pass
        committed = await subagents.commit_knowledge_if_approved(
            kind="incident_recipe",
            title=f"Рецепт инцидента: {signature[:72] or inc_id}",
            body=body,
            tags=["lifelong", "incident", "self-heal"],
            source_trace=inc_id,
            chat=chat,
        )
        await db.execute(
            "INSERT INTO agent_achievements (id,title,detail,category,importance) VALUES (?,?,?,?,?)",
            (db.new_id(), f"🧩 Усвоен инцидент: {signature[:60]}", body[:300], "self-heal", 0.65),
        )
        return {"incident": inc, "committed": committed}
    return None


async def learning_iteration(iteration: int = 0, *, chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """Одна итерация обучения: incidents → Critic-gate → topic rule."""
    trace = db.new_id()
    mined = await _mine_incident(iteration, chat=chat)
    if mined is not None:
        await db.execute(
            "INSERT INTO episodic_memory_logs (id,decision_trace,entry_type,content) VALUES (?,?,?,?)",
            (db.new_id(), trace, "learning", "Lifelong Learning усвоил resolved incident в cognitive graph."),
        )
        return {"mode": "incident", "mined": mined, "trace": trace}

    topic = _pick_topic(iteration)
    await db.execute(
        "INSERT INTO episodic_memory_logs (id,decision_trace,entry_type,content) VALUES (?,?,?,?)",
        (db.new_id(), trace, "hypothesis", f"Обучение [{subagents.EXISTENTIAL_DIRECTIVE}] тема: {topic}"),
    )

    body = _BASELINE_RULES.get(topic)
    title = f"Правило sysadmin: {topic[:60]}"
    if chat is not None:
        try:
            body = (await subagents.run_role(
                "researcher",
                f"Сформулируй ОДНО конкретное проверяемое правило по теме «{topic}» в контексте: {subagents.EXISTENTIAL_DIRECTIVE} Только текст правила.",
                chat=chat,
            )).get("content") or body
        except Exception:  # noqa: BLE001
            pass
    if not body:
        return {"mode": "topic", "topic": topic, "committed": None, "trace": trace}

    committed = await subagents.commit_knowledge_if_approved(
        kind="rule", title=title, body=body, tags=["lifelong", "sysadmin"],
        source_trace=trace, chat=chat)
    await db.execute(
        "INSERT INTO agent_achievements (id,title,detail,category,importance) VALUES (?,?,?,?,?)",
        (db.new_id(), f"📚 Изучено: {topic[:60]}", body[:300], "research", 0.4))
    return {"mode": "topic", "topic": topic, "committed": committed, "trace": trace}


_chat_provider: Optional[Callable[[], Optional[ChatFn]]] = None


def set_chat_provider(provider: Callable[[], Optional[ChatFn]]) -> None:
    global _chat_provider
    _chat_provider = provider


async def _worker(checkpoint: dict[str, Any], should_suspend: asyncio.Event) -> dict[str, Any]:
    i = int(checkpoint.get("iteration", 0))
    chat = _chat_provider() if _chat_provider else None
    while not should_suspend.is_set():
        try:
            await learning_iteration(i, chat=chat)
        except Exception as exc:  # noqa: BLE001
            log.warning("Итерация обучения #%d упала: %s", i, exc)
        i += 1
        if i % 10 == 0:
            try:
                from . import maintenance
                await maintenance.sleep_cycle()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(should_suspend.wait(), timeout=ITER_PAUSE_SEC)
        except asyncio.TimeoutError:
            pass
    return {"iteration": i, "suspended_at": time.time()}


handle = suspend.BackgroundLoopHandle(_worker)


async def start() -> None:
    await handle.start()


async def on_user_activity(active_user: str = "local-admin", goal: str = "") -> None:
    suspend.note_user_activity()
    await handle.suspend(active_user=active_user, goal=goal)


async def maybe_resume() -> bool:
    if suspend.should_resume():
        await handle.resume()
        return True
    return False


async def status() -> dict[str, Any]:
    st = await suspend.get_state()
    return {
        "enabled": ENABLED,
        "state": st.get("state", "IDLE"),
        "checkpoint": st.get("checkpoint"),
        "idle_for_sec": round(suspend.user_idle_for(), 1),
        "mine_incidents": MINE_INCIDENTS,
    }
