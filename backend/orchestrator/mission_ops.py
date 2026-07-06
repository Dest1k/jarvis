# -*- coding: utf-8 -*-
"""mission_ops.py — explicit mission/autonomy tool layer for JARVIS.

The normal ReAct loop is excellent for one conversational turn. This tool gives
Core JARVIS a durable structure for broad goals: decompose into cognitive project
plans, run role briefs, inspect task status, and trigger one safe learning tick.
"""

from __future__ import annotations

import json
from typing import Any

from .tools import Tool, ToolContext


async def _ensure_db() -> None:
    from cognitive_core import db
    await db.connect()


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
            "SELECT id,goal,owner_id,status,created_at,updated_at FROM project_plans ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        tasks = await db.query(
            "SELECT id,plan_id,title,assignee,status,order_index FROM project_tasks ORDER BY plan_id,order_index LIMIT ?",
            (limit * 5,),
        )
        return {"ok": True, "content": json.dumps({"plans": plans, "tasks": tasks[:limit * 5]}, ensure_ascii=False, indent=2), "data": {"plans": plans, "tasks": tasks}}

    if action == "run_role":
        role = str(args.get("role") or "researcher").strip().lower()
        task = str(args.get("task") or goal).strip()
        context = str(args.get("context") or "")[:12000]
        if role not in ("researcher", "coder", "critic", "orchestrator"):
            return {"ok": False, "content": "role должен быть researcher|coder|critic|orchestrator."}
        if not task:
            return {"ok": False, "content": "Нужна task для run_role."}
        if role == "critic":
            from cognitive_core import subagents
            review = await subagents.critic_review(kind="mission_task", title=task[:100], body=context or task)
            return {"ok": review.get("verdict") == "approved", "content": json.dumps(review, ensure_ascii=False, indent=2), "data": review}
        from cognitive_core import subagents
        from . import llm
        res = await subagents.run_role(role, task, chat=llm.chat, context=context)
        return {"ok": bool(res.get("ok")), "content": str(res.get("content", ""))[:6000], "data": res}

    if action == "learning_tick":
        from cognitive_core import learning
        from . import llm
        iteration = int(args.get("iteration", 0) or 0)
        res = await learning.learning_iteration(iteration, chat=llm.chat)
        return {"ok": True, "content": json.dumps(res, ensure_ascii=False, indent=2)[:6000], "data": res}

    return {"ok": False, "content": "Неизвестное mission action. Доступно: plan|status|run_role|learning_tick."}


def register(registry: Any) -> None:
    registry.add(Tool(
        "mission",
        "Автономная работа с широкими целями: decompose goal в durable project plan, status планов, run_role суб-агентов, learning_tick. Используй для больших задач, а не пытайся держать всё в одном ответе.",
        {"action": "plan|status|run_role|learning_tick", "goal": "цель", "task": "подзадача", "role": "researcher|coder|critic", "context": "контекст", "limit": "лимит"},
        tool_mission,
    ))
