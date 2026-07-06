# -*- coding: utf-8 -*-
"""mission_ops.py — explicit mission/autonomy tool layer for JARVIS.

The normal ReAct loop is excellent for one conversational turn. This tool gives
Core JARVIS a durable structure for broad goals: decompose into cognitive project
plans, execute runnable tasks, run role briefs, inspect task status, and trigger
one safe learning tick.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .tools import Tool, ToolContext

ROLES = ("researcher", "coder", "critic", "orchestrator")


async def _ensure_db() -> None:
    from cognitive_core import db
    await db.connect()


def _loads_list(raw: str) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


async def _role_execute(role: str, title: str, context: str) -> dict[str, Any]:
    from cognitive_core import subagents
    from . import llm
    if role == "critic":
        review = await subagents.critic_review(kind="mission_task", title=title[:100], body=context or title, chat=llm.chat)
        return {"ok": review.get("verdict") == "approved", "role": role, "content": json.dumps(review, ensure_ascii=False), "review": review}
    if role == "orchestrator":
        role = "researcher"
    return await subagents.run_role(role, title, chat=llm.chat, context=context)


async def _execute_one(plan_id: str | None, *, limit_context: int = 10000) -> dict[str, Any]:
    from cognitive_core import db
    where = "WHERE status='todo'"
    params: list[Any] = []
    if plan_id:
        where += " AND plan_id=?"
        params.append(plan_id)
    tasks = await db.query(
        f"SELECT * FROM project_tasks {where} ORDER BY plan_id, order_index LIMIT 50",
        params,
    )
    done = {r["id"] for r in await db.query("SELECT id FROM project_tasks WHERE status='done'")}
    runnable = None
    for task in tasks:
        deps = _loads_list(task.get("depends_on", "[]"))
        if all(d in done for d in deps):
            runnable = task
            break
    if runnable is None:
        if plan_id:
            left = await db.query_one("SELECT COUNT(*) AS n FROM project_tasks WHERE plan_id=? AND status NOT IN ('done','failed')", (plan_id,))
            if (left or {}).get("n", 0) == 0:
                await db.execute("UPDATE project_plans SET status='done' WHERE id=?", (plan_id,))
        return {"ok": False, "reason": "no runnable task", "plan_id": plan_id}

    tid = runnable["id"]
    role = str(runnable.get("assignee") or "orchestrator")
    if role not in ROLES:
        role = "orchestrator"
    await db.execute("UPDATE project_tasks SET status='in_progress', progress=0.2 WHERE id=?", (tid,))
    plan = await db.query_one("SELECT * FROM project_plans WHERE id=?", (runnable["plan_id"],)) or {}
    previous = await db.query(
        "SELECT title,result,status FROM project_tasks WHERE plan_id=? AND order_index<? ORDER BY order_index",
        (runnable["plan_id"], runnable.get("order_index", 0)),
    )
    context = ("Mission goal: " + str(plan.get("goal", "")) + "\nPrevious results:\n" +
               json.dumps(previous, ensure_ascii=False, indent=2))[:limit_context]
    try:
        res = await _role_execute(role, str(runnable.get("title") or ""), context)
        ok = bool(res.get("ok"))
        content = str(res.get("content") or json.dumps(res, ensure_ascii=False))[:8000]
        await db.execute(
            "UPDATE project_tasks SET status=?, progress=?, result=? WHERE id=?",
            ("done" if ok else "failed", 1.0 if ok else 0.0, content, tid),
        )
        await db.execute(
            "INSERT INTO episodic_memory_logs (id,decision_trace,session_id,task_id,step_index,entry_type,content,outcome) VALUES (?,?,?,?,?,?,?,?)",
            (db.new_id(), f"mission:{runnable['plan_id']}", "mission", tid, int(runnable.get("order_index", 0)), "success" if ok else "failure", content[:2000], "success" if ok else "failure"),
        )
        remaining = await db.query_one(
            "SELECT COUNT(*) AS n FROM project_tasks WHERE plan_id=? AND status IN ('todo','in_progress','blocked')",
            (runnable["plan_id"],),
        )
        if ok and (remaining or {}).get("n", 0) == 0:
            await db.execute("UPDATE project_plans SET status='done' WHERE id=?", (runnable["plan_id"],))
        return {"ok": ok, "task": runnable, "result": res}
    except Exception as exc:  # noqa: BLE001
        await db.execute("UPDATE project_tasks SET status='failed', progress=0.0, result=? WHERE id=?", (str(exc)[:2000], tid))
        return {"ok": False, "task": runnable, "error": str(exc)}


async def tool_mission(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = str(args.get("action") or "plan").strip().lower()
    goal = str(args.get("goal") or args.get("task") or "").strip()
    owner = str(args.get("owner") or "local-admin").strip() or "local-admin"
    limit = max(1, min(int(args.get("limit", 20) or 20), 100))

    try:
        await _ensure_db()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Cognitive DB недоступна: {exc}"}

    if action in ("plan", "decompose"):
        if not goal:
            return {"ok": False, "content": "Нужна цель goal/task для mission plan."}
        from cognitive_core import subagents
        from . import llm
        plan = await subagents.decompose_goal(goal, owner_id=owner, chat=llm.chat)
        tasks = plan.get("tasks", [])
        lines = [f"План создан: {plan.get('plan_id')} · {goal}"]
        for i, t in enumerate(tasks, 1):
            deps = t.get("depends_on") or []
            dep_txt = f" deps={len(deps)}" if deps else ""
            lines.append(f"{i}. [{t.get('assignee')}] {t.get('title')}{dep_txt}")
        return {"ok": True, "content": "\n".join(lines), "data": plan}

    if action == "status":
        from cognitive_core import db
        plans = await db.query(
            "SELECT id,goal,owner_id,status,created_at FROM project_plans ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        tasks = await db.query(
            "SELECT id,plan_id,title,assignee,status,progress,order_index,result FROM project_tasks ORDER BY plan_id,order_index LIMIT ?",
            (limit * 5,),
        )
        return {"ok": True, "content": json.dumps({"plans": plans, "tasks": tasks[:limit * 5]}, ensure_ascii=False, indent=2), "data": {"plans": plans, "tasks": tasks}}

    if action in ("execute", "run_next", "step"):
        plan_id = str(args.get("plan_id") or "").strip() or None
        rounds = max(1, min(int(args.get("rounds", 1) or 1), 10))
        results = []
        for _ in range(rounds):
            res = await _execute_one(plan_id)
            results.append(res)
            if not res.get("ok"):
                break
        return {"ok": any(r.get("ok") for r in results), "content": json.dumps(results, ensure_ascii=False, indent=2)[:8000], "data": {"results": results, "ts": time.time()}}

    if action == "run_role":
        role = str(args.get("role") or "researcher").strip().lower()
        task = str(args.get("task") or goal).strip()
        context = str(args.get("context") or "")[:12000]
        if role not in ROLES:
            return {"ok": False, "content": "role должен быть researcher|coder|critic|orchestrator."}
        if not task:
            return {"ok": False, "content": "Нужна task для run_role."}
        res = await _role_execute(role, task, context)
        return {"ok": bool(res.get("ok")), "content": str(res.get("content", ""))[:6000], "data": res}

    if action == "learning_tick":
        from cognitive_core import learning
        from . import llm
        iteration = int(args.get("iteration", 0) or 0)
        res = await learning.learning_iteration(iteration, chat=llm.chat)
        return {"ok": True, "content": json.dumps(res, ensure_ascii=False, indent=2)[:6000], "data": res}

    return {"ok": False, "content": "Неизвестное mission action. Доступно: plan|status|execute|run_role|learning_tick."}


def register(registry: Any) -> None:
    registry.add(Tool(
        "mission",
        "Автономная работа с широкими целями: plan/decompose в durable project plan, execute следующей runnable-задачи, status планов, run_role суб-агентов, learning_tick. Используй для больших задач.",
        {"action": "plan|status|execute|run_role|learning_tick", "goal": "цель", "plan_id": "id плана", "rounds": "1..10", "task": "подзадача", "role": "researcher|coder|critic|orchestrator", "context": "контекст", "limit": "лимит"},
        tool_mission,
    ))
