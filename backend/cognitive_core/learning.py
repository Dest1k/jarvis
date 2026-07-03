# -*- coding: utf-8 -*-
"""
learning.py — фоновый цикл «Lifelong Learning» JARVIS OS.

Экзистенциальная директива (§0): «Чего мне ещё не хватает, чтобы стать
идеальным системным администратором и ассистентом?» На простое системы воркер
делает итерации самосовершенствования, ЯКОРЯ их вокруг высокоуровневого
системного администрирования, автоматизации и ассистент-паттернов.

Zero-latency: воркер построен на suspend.BackgroundLoopHandle — как только
пользователь пишет, on_user_activity() МГНОВЕННО приостанавливает цикл
(чекпоинт в БД, обрыв стрима vLLM → VRAM пользователю). По простою — resume.

Одна итерация (learning_iteration):
    1. Выбрать под-цель обучения (ротация тем sysadmin + директива).
    2. (Опц., если есть LLM) исследовать/сформулировать правило Researcher-ролью.
       Без LLM — детерминированный кандидат из базы лучших практик (рабочий
       каркас, чтобы цикл был осмысленным и без модели).
    3. Прогнать кандидата через Critic-гейт → при approved закоммитить в граф.
    4. Залогировать эпизод + веху достижения.

Всё — идемпотентно и best-effort; ошибки итерации не валят воркер.
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

# Темы, вокруг которых якорится обучение (высокоуровневый sysadmin/ассистент).
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

# Детерминированная база кандидатов (используется без LLM) — безопасные правила.
_BASELINE_RULES = {
    "мониторинг и алертинг (Prometheus/Grafana/Alertmanager)":
        "Каждый критичный сервис должен экспортировать метрики и иметь алерт на "
        "доступность (up==0) и на насыщение ресурса (CPU/RAM/диск >85%).",
    "автоматизация конфигураций (Ansible/idempotent playbooks)":
        "Все изменения конфигурации выполнять идемпотентными плейбуками с "
        "--check/--diff перед применением; ручные правки на проде запрещены.",
    "безопасность хоста (firewall, least-privilege, аудит)":
        "По умолчанию deny на входящий трафик; открывать только нужные порты; "
        "sudo — только именованным операторам с логированием команд.",
    "резервное копирование и восстановление (снапшоты, проверка бэкапов)":
        "Бэкап без проверенного восстановления не считается бэкапом — регулярно "
        "прогонять restore-тест в изолированной среде.",
}


def _pick_topic(iteration: int) -> str:
    return _TOPICS[iteration % len(_TOPICS)]


async def learning_iteration(iteration: int = 0, *,
                             chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """Одна итерация обучения: сформулировать → Critic-гейт → закоммитить."""
    topic = _pick_topic(iteration)
    trace = db.new_id()
    await db.execute(
        "INSERT INTO episodic_memory_logs (id,decision_trace,entry_type,content) VALUES (?,?,?,?)",
        (db.new_id(), trace, "hypothesis",
         f"Обучение [{subagents.EXISTENTIAL_DIRECTIVE}] тема: {topic}"))

    body = _BASELINE_RULES.get(topic)
    title = f"Правило sysadmin: {topic[:60]}"
    if chat is not None:
        try:
            body = (await subagents.run_role(
                "researcher",
                f"Сформулируй ОДНО конкретное проверяемое правило по теме «{topic}» "
                f"в контексте: {subagents.EXISTENTIAL_DIRECTIVE} Только текст правила.",
                chat=chat)).get("content") or body
        except Exception:  # noqa: BLE001
            pass
    if not body:
        # тема без baseline и без LLM — пропускаем коммит, но фиксируем размышление
        return {"topic": topic, "committed": None, "trace": trace}

    committed = await subagents.commit_knowledge_if_approved(
        kind="rule", title=title, body=body, tags=["lifelong", "sysadmin"],
        source_trace=trace, chat=chat)
    await db.execute(
        "INSERT INTO agent_achievements (id,title,detail,category,importance) VALUES (?,?,?,?,?)",
        (db.new_id(), f"📚 Изучено: {topic[:60]}", body[:300], "research", 0.4))
    return {"topic": topic, "committed": committed, "trace": trace}


# --------------------------------------------------------------------------- #
# Воркер на suspend/resume
# --------------------------------------------------------------------------- #
_chat_provider: Optional[Callable[[], Optional[ChatFn]]] = None


def set_chat_provider(provider: Callable[[], Optional[ChatFn]]) -> None:
    """Инъекция провайдера chat-функции диспетчера (ленивая, из server.py)."""
    global _chat_provider
    _chat_provider = provider


async def _worker(checkpoint: dict[str, Any], should_suspend: asyncio.Event) -> dict[str, Any]:
    """Тело фонового цикла: итерации обучения с мгновенной приостановкой."""
    i = int(checkpoint.get("iteration", 0))
    chat = _chat_provider() if _chat_provider else None
    while not should_suspend.is_set():
        try:
            await learning_iteration(i, chat=chat)
        except Exception as exc:  # noqa: BLE001
            log.warning("Итерация обучения #%d упала: %s", i, exc)
        i += 1
        # периодически — sleep-cycle консолидация
        if i % 10 == 0:
            try:
                from . import maintenance
                await maintenance.sleep_cycle()
            except Exception:  # noqa: BLE001
                pass
        # прерываемая пауза: реагируем на suspend в течение ITER_PAUSE_SEC
        try:
            await asyncio.wait_for(should_suspend.wait(), timeout=ITER_PAUSE_SEC)
        except asyncio.TimeoutError:
            pass
    return {"iteration": i, "suspended_at": time.time()}


handle = suspend.BackgroundLoopHandle(_worker)


async def start() -> None:
    """Запустить/возобновить фоновый цикл обучения."""
    await handle.start()


async def on_user_activity(active_user: str = "local-admin", goal: str = "") -> None:
    """
    Мгновенно приостановить обучение при активности пользователя (zero-latency):
    чекпоинт + обрыв стрима vLLM → VRAM пользователю.
    """
    suspend.note_user_activity()
    await handle.suspend(active_user=active_user, goal=goal)


async def maybe_resume() -> bool:
    """Возобновить обучение, если пользователь простаивает дольше порога."""
    if suspend.should_resume():
        await handle.resume()
        return True
    return False


async def status() -> dict[str, Any]:
    st = await suspend.get_state()
    return {"enabled": ENABLED, "state": st.get("state", "IDLE"),
            "checkpoint": st.get("checkpoint"),
            "idle_for_sec": round(suspend.user_idle_for(), 1)}
