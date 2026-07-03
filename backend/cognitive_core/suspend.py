# -*- coding: utf-8 -*-
"""
suspend.py — механизм Zero-Latency Suspend/Resume фонового цикла обучения.

Директива: как только пользователь присылает промпт, фоновый «Lifelong Learning»
цикл обязан МГНОВЕННО уступить ресурсы — не через отмену «когда-нибудь», а
детерминированно за один тик:

    1. Выставить состояние SUSPENDED в БД.
    2. Сохранить чекпоинт (цель, шаг, скрэтч, id decision_trace) — чтобы потом
       продолжить ровно с места остановки.
    3. НЕМЕДЛЕННО закрыть исходящий HTTP-стрим к vLLM → освобождается KV-cache
       и VRAM ассистента ПОЛНОСТЬЮ отдаётся пользовательскому ходу.
    4. Отдать управление (event внутри asyncio) — без ожидания «дочистки».

Ключ к «zero-latency»: фоновый воркер держит asyncio.Task и одно активное
соединение стрима. suspend() ставит Event и закрывает стрим (aclose) — воркер
на следующем await ловит CancelledError/закрытый стрим, атомарно чекпоинтит и
переходит в SUSPENDED. Пользовательский ход не ждёт ничего.

resume() поднимает воркер обратно из чекпоинта, когда пользователь простаивает
дольше idle-порога.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

from . import db

# Кто-то один владеет фоновым циклом на процесс.
_resume_after_idle_sec = 20.0
_last_user_activity: float = 0.0


class BackgroundLoopHandle:
    """
    Обёртка над фоновым циклом обучения с мгновенной приостановкой.

    worker_factory(checkpoint, should_suspend) → корутина, которая периодически
    проверяет should_suspend.is_set() на каждом шаге и корректно завершается,
    вернув свой чекпоинт (dict) для сохранения.
    """

    def __init__(self, worker_factory: Callable[[dict[str, Any], asyncio.Event],
                                                 Awaitable[dict[str, Any]]]) -> None:
        self._factory = worker_factory
        self._task: Optional[asyncio.Task] = None
        self._suspend = asyncio.Event()
        # закрыватель активного vLLM-стрима (устанавливается воркером при открытии)
        self._stream_closer: Optional[Callable[[], Awaitable[None]]] = None

    def set_stream_closer(self, closer: Optional[Callable[[], Awaitable[None]]]) -> None:
        """Воркер регистрирует, как немедленно оборвать текущий стрим к vLLM."""
        self._stream_closer = closer

    async def start(self) -> None:
        """Запустить/возобновить фоновый цикл из чекпоинта в БД."""
        if self._task is not None and not self._task.done():
            return
        self._suspend.clear()
        checkpoint = await _load_checkpoint()
        await _set_state("THINKING", checkpoint=checkpoint)
        self._task = asyncio.create_task(self._run(checkpoint))

    async def _run(self, checkpoint: dict[str, Any]) -> None:
        try:
            final_cp = await self._factory(checkpoint, self._suspend)
            await _save_checkpoint(final_cp or {})
            if not self._suspend.is_set():
                await _set_state("IDLE")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await _set_state("RECOVERING", note=f"loop error: {exc}")

    async def suspend(self, active_user: str, goal: str = "") -> None:
        """
        МГНОВЕННАЯ приостановка: чекпоинт → обрыв стрима vLLM (освобождение
        VRAM/KV) → SUSPENDED. Не ждём завершения тяжёлых операций воркера.
        """
        self._suspend.set()
        # 1) немедленно оборвать активный стрим к vLLM — освободить KV/VRAM
        if self._stream_closer is not None:
            try:
                await self._stream_closer()
            except Exception:  # noqa: BLE001
                pass
        # 2) отметить состояние (чекпоинт воркер допишет на своём тике; здесь
        #    фиксируем намерение и активного пользователя без ожидания)
        await _set_state("SUSPENDED", active_user=active_user, goal=goal)
        # 3) отменить задачу, если она застряла в неотменяемой секции дольше тика
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def resume(self) -> None:
        await self.start()


# --------------------------------------------------------------------------- #
# Взаимодействие с состоянием пользователя (idle-детект)
# --------------------------------------------------------------------------- #
def note_user_activity() -> None:
    """Отметить активность пользователя (сбрасывает таймер простоя)."""
    global _last_user_activity
    _last_user_activity = time.time()


def user_idle_for() -> float:
    return time.time() - _last_user_activity if _last_user_activity else 1e9


def should_resume() -> bool:
    return user_idle_for() >= _resume_after_idle_sec


# --------------------------------------------------------------------------- #
# Персистентность состояния/чекпоинта (единственная строка id=1)
# --------------------------------------------------------------------------- #
async def _set_state(state: str, *, checkpoint: Optional[dict[str, Any]] = None,
                     active_user: Optional[str] = None, goal: Optional[str] = None,
                     note: str = "") -> None:
    cur = await db.query_one("SELECT state FROM agent_cognitive_state WHERE id = 1")
    prev = cur["state"] if cur else "IDLE"
    fields = ["state=?", "prev_state=?", "updated_at=unixepoch('subsec')"]
    params: list[Any] = [state, prev]
    if checkpoint is not None:
        fields.append("checkpoint=?"); params.append(json.dumps(checkpoint, ensure_ascii=False))
    if active_user is not None:
        fields.append("active_user=?"); params.append(active_user)
    if goal is not None:
        fields.append("active_goal=?"); params.append(goal)
    await db.execute(f"UPDATE agent_cognitive_state SET {', '.join(fields)} WHERE id = 1", params)


async def _save_checkpoint(cp: dict[str, Any]) -> None:
    await db.execute(
        "UPDATE agent_cognitive_state SET checkpoint=?, updated_at=unixepoch('subsec') WHERE id=1",
        (json.dumps(cp, ensure_ascii=False),))


async def _load_checkpoint() -> dict[str, Any]:
    row = await db.query_one("SELECT checkpoint FROM agent_cognitive_state WHERE id = 1")
    if not row or not row.get("checkpoint"):
        return {}
    try:
        return json.loads(row["checkpoint"])
    except (json.JSONDecodeError, TypeError):
        return {}


async def get_state() -> dict[str, Any]:
    return await db.query_one("SELECT * FROM agent_cognitive_state WHERE id = 1") or {}
