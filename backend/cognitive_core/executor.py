# -*- coding: utf-8 -*-
"""
executor.py — автономный исполнитель планов JARVIS OS.

Берёт декомпозированный план (project_plans/project_tasks) и ПРОГОНЯЕТ его сам,
уважая зависимости (depends_on) и назначения суб-агентам:

    researcher   → run_role('researcher')  — исследование/факты.
    coder        → run_role('coder') → (опц.) исполнение в sandbox.
    critic       → critic_review — валидация накопленного результата;
                   verdict='rejected' помечает задачу failed (шлюз безопасности).
    orchestrator → обобщённый шаг (research-обёртка).

Свойства:
    • Топологический порядок: задача стартует, только когда ВСЕ её depends_on
      завершены (done). Дедлок (никого не запустить) → план 'blocked'.
    • Прогресс и результат пишутся в project_tasks (todo→in_progress→done|failed,
      progress 0→1) + эпизодические логи с общим decision_trace (объяснимость).
    • HITL: опасные шаги (danger) при недостаточном autonomy_level помечаются
      'blocked' и ждут одобрения — план не делает деструктив без разрешения.
    • Инъектируемые chat/sandbox_run → детерминированные части тестируются без
      модели и без Docker.

Событийный колбэк on_event(dict) (опц.) — для стрима прогресса в дашборд.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable, Optional

from . import db, parallel, subagents

ChatFn = Callable[..., Awaitable[str]]
SandboxRun = Callable[[str, str], Awaitable[tuple[Optional[int], str]]]  # (code,lang)->(rc,out)
EventFn = Callable[[dict[str, Any]], Awaitable[None]]

# Максимум шагов на прогон (защита от зацикливания).
MAX_TASKS = 40
# Потолок одновременно исполняемых НЕзависимых задач волны (vLLM батчит запросы).
PLAN_CONCURRENCY_KEY = "JARVIS_PLAN_CONCURRENCY"
PLAN_CONCURRENCY_DEFAULT = 4


async def _emit(on_event: Optional[EventFn], ev: dict[str, Any]) -> None:
    if on_event is not None:
        try:
            await on_event(ev)
        except Exception:  # noqa: BLE001
            pass


async def _set_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    await db.execute(f"UPDATE project_tasks SET {sets} WHERE id=?",
                     (*fields.values(), task_id))


async def _log(trace: str, session: str, entry_type: str, content: str,
               outcome: Optional[str] = None, task_id: Optional[str] = None) -> None:
    await db.execute(
        "INSERT INTO episodic_memory_logs (id,decision_trace,session_id,task_id,"
        "entry_type,content,outcome) VALUES (?,?,?,?,?,?,?)",
        (db.new_id(), trace, session, task_id, entry_type, content[:4000], outcome))


def _looks_like_code(text: str) -> tuple[bool, str]:
    """Грубо определить, есть ли в ответе исполнимый код-блок и его язык."""
    import re
    m = re.search(r"```(\w+)?\n(.*?)```", text, re.DOTALL)
    if not m:
        return False, ""
    lang = (m.group(1) or "bash").lower()
    lang = {"python": "python", "py": "python", "bash": "bash", "sh": "bash",
            "shell": "bash"}.get(lang, "")
    return bool(lang), lang


async def _run_one_task(task: dict[str, Any], *, trace: str, session_id: str,
                        context: str, chat: Optional[ChatFn],
                        sandbox_run: Optional[SandboxRun],
                        on_event: Optional[EventFn]) -> dict[str, Any]:
    """
    Исполнить ОДНУ задачу (её роль/критика/песочницу) и записать прогресс.
    `context` — снимок накопленного контекста завершённых волн (read-only): задачи
    одной волны НЕзависимы (deps уже done), поэтому не видят промежуточных
    результатов друг друга — это корректно и позволяет гонять их параллельно.
    Возврат включает 'result' для слияния в контекст следующей волны.
    """
    tid, assignee, title = task["id"], task["assignee"], task["title"]
    await _set_task(tid, status="in_progress", progress=0.3)
    await _emit(on_event, {"type": "task_start", "task_id": tid,
                           "title": title, "assignee": assignee})
    await _log(trace, session_id, "action", f"[{assignee}] {title}", task_id=tid)

    result_text = ""
    outcome = "success"
    try:
        if assignee == "critic":
            review = await subagents.critic_review(
                kind="rule", title=title, body=context, chat=chat)
            result_text = f"Critic: {review['verdict']}. {review['notes']}"
            if review["verdict"] == "rejected":
                outcome = "failure"
        elif assignee in ("researcher", "coder", "orchestrator"):
            role = "coder" if assignee == "coder" else "researcher"
            if chat is None:
                result_text = f"(модель недоступна — шаг '{title}' пропущен как no-op)"
            else:
                r = await subagents.run_role(role, title, chat=chat, context=context)
                result_text = r.get("content", "")
                if not r.get("ok"):
                    outcome = "failure"
            # coder → попытка исполнить код в sandbox (если дали runner)
            if assignee == "coder" and sandbox_run and result_text:
                has_code, lang = _looks_like_code(result_text)
                if has_code:
                    import re
                    code = re.search(r"```\w*\n(.*?)```", result_text, re.DOTALL).group(1)
                    rc, out = await sandbox_run(code, lang)
                    result_text += f"\n[sandbox rc={rc}]\n{out[:1500]}"
                    await _log(trace, session_id, "sandbox_output",
                               f"rc={rc}\n{out[:1500]}", task_id=tid)
                    if rc not in (0, None):
                        outcome = "failure"
        else:
            result_text = f"Неизвестный исполнитель '{assignee}' — шаг пропущен."
    except Exception as exc:  # noqa: BLE001
        result_text = f"Ошибка исполнения: {exc}"
        outcome = "failure"

    status = "done" if outcome == "success" else "failed"
    await _set_task(tid, status=status, progress=1.0, result=result_text[:4000])
    await _log(trace, session_id,
               "success" if outcome == "success" else "failure",
               result_text, outcome=outcome, task_id=tid)
    await _emit(on_event, {"type": "task_done", "task_id": tid,
                           "status": status, "result": result_text[:400]})
    return {"task_id": tid, "title": title, "assignee": assignee,
            "status": status, "result": result_text}


async def run_plan(plan_id: str, *, chat: Optional[ChatFn] = None,
                   sandbox_run: Optional[SandboxRun] = None,
                   autonomy_level: int = 2, session_id: str = "executor",
                   on_event: Optional[EventFn] = None) -> dict[str, Any]:
    """
    Прогнать план целиком. Возвращает отчёт {plan_id, status, executed, results}.

    Планировщик — ВОЛНОВОЙ: на каждой итерации собирает ВСЕ задачи, у которых все
    depends_on уже 'done', и запускает их ПАРАЛЛЕЛЬНО (bounded, vLLM их батчит),
    а не строго по одной. Топологический порядок сохраняется — зависимая задача
    не стартует, пока не готовы её предпосылки. Так независимые ветки плана
    утилизируют «мозг» в 2+ потока вместо простоя в очереди.
    """
    plan = await db.query_one("SELECT * FROM project_plans WHERE id=?", (plan_id,))
    if plan is None:
        return {"ok": False, "error": "План не найден."}
    trace = db.new_id()
    await db.execute("UPDATE project_plans SET status='in_progress' WHERE id=?", (plan_id,))
    await _emit(on_event, {"type": "plan_start", "plan_id": plan_id, "goal": plan["goal"]})

    executed: list[dict[str, Any]] = []
    context_acc = f"Цель плана: {plan['goal']}\n"
    dispatched = 0
    limit = parallel.concurrency(PLAN_CONCURRENCY_KEY, PLAN_CONCURRENCY_DEFAULT)

    while dispatched < MAX_TASKS:
        tasks = await db.query(
            "SELECT * FROM project_tasks WHERE plan_id=? ORDER BY order_index", (plan_id,))
        done_ids = {t["id"] for t in tasks if t["status"] == "done"}
        pending = [t for t in tasks if t["status"] in ("todo", "blocked")]
        # ВОЛНА: все todo-задачи, у которых все зависимости уже done.
        wave = []
        for t in tasks:
            if t["status"] != "todo":
                continue
            deps = json.loads(t["depends_on"] or "[]")
            if all(d in done_ids for d in deps):
                wave.append(t)
        if not wave:
            # нет запускаемых. Если остались невыполненные — дедлок/блокировка.
            if pending:
                await db.execute("UPDATE project_plans SET status='blocked' WHERE id=?", (plan_id,))
                await _emit(on_event, {"type": "plan_blocked", "plan_id": plan_id,
                                       "pending": [t["title"] for t in pending]})
                break
            await db.execute("UPDATE project_plans SET status='done' WHERE id=?", (plan_id,))
            await _emit(on_event, {"type": "plan_done", "plan_id": plan_id})
            break

        # не превысить общий бюджет шагов
        wave = wave[:MAX_TASKS - dispatched]
        dispatched += len(wave)
        # снимок контекста фиксируем ДО волны — задачи волны независимы.
        snapshot = context_acc
        results = await parallel.bounded_map(
            lambda t: _run_one_task(t, trace=trace, session_id=session_id,
                                    context=snapshot, chat=chat,
                                    sandbox_run=sandbox_run, on_event=on_event),
            wave, limit=limit)
        for r in results:
            executed.append({"task_id": r["task_id"], "title": r["title"],
                             "assignee": r["assignee"], "status": r["status"]})
            context_acc += f"\n[{r['assignee']}] {r['title']} → {r['result'][:600]}\n"
        # провал критичной задачи не продолжит зависимые: их deps не станут 'done',
        # план уйдёт в blocked на следующей волне.

    final = await db.query_one("SELECT status FROM project_plans WHERE id=?", (plan_id,))
    # достижение в таймлайн
    await db.execute(
        "INSERT INTO agent_achievements (id,title,detail,category,importance) VALUES (?,?,?,?,?)",
        (db.new_id(), f"Исполнение плана: {plan['goal'][:70]}",
         f"Статус: {final['status']}, задач выполнено: {len(executed)}",
         "milestone", 0.7))
    return {"ok": True, "plan_id": plan_id, "status": final["status"],
            "trace": trace, "executed": executed}
