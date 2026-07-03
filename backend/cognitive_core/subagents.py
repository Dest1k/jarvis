# -*- coding: utf-8 -*-
"""
subagents.py — оркестрация суб-агентов JARVIS OS.

Главный Orchestrator декомпозирует высокоуровневую цель в задачи с зависимостями
(project_plans/project_tasks) и раздаёт специализированным суб-агентам:

    Researcher-Agent — исследует IT/sysadmin-концепции (веб/доки).
    Coder-Agent      — пишет/тестирует код и скрипты (sandbox).
    Critic-Agent     — ВАЛИДИРУЕТ безопасность/логику КАЖДОГО навыка/правила
                       перед коммитом в граф знаний (обязательный шлюз).
    Recovery-Agent   — анализирует traceback, чинит зависимости/сервисы.

Экзистенциальная директива (§0): «Чего мне ещё не хватает, чтобы стать идеальным
системным администратором и ассистентом?» — якорь для Researcher/decompose.

Critic реализован ДВУХСЛОЙНО:
    1. Детерминированные ПРАВИЛА безопасности (offline, мгновенно, тестируемо) —
       ловят катастрофические паттерны (rm -rf /, mkfs, dd на диск, fork-бомбы,
       эксфильтрация секретов). Это жёсткий предохранитель.
    2. LLM-ревью логической состоятельности (опционально, если доступна модель).

Функции, требующие LLM (researcher/coder/llm-critic), принимают инъектируемый
`chat`-коллбэк — поэтому детерминированную часть можно тестировать без модели.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable, Optional

from . import db

ChatFn = Callable[..., Awaitable[str]]

EXISTENTIAL_DIRECTIVE = (
    "Чего мне ещё не хватает, чтобы стать идеальным системным администратором "
    "и ассистентом?")

# --------------------------------------------------------------------------- #
# Critic — детерминированные правила безопасности (жёсткий предохранитель)
# --------------------------------------------------------------------------- #
# Катастрофические паттерны: при совпадении навык/правило ОТКЛОНЯЕТСЯ безусловно.
_CATASTROPHIC = [
    (r"\brm\s+-[rf]{1,2}\s+/(?:\s|$|\*)", "рекурсивное удаление корня (rm -rf /)"),
    (r"\bmkfs\b", "форматирование ФС (mkfs)"),
    (r"\bdd\b.*\bof=/dev/(sd|nvme|vd)", "перезапись блочного устройства (dd of=/dev/...)"),
    (r":\(\)\s*\{\s*:\|\:&\s*\}\s*;", "fork-бомба"),
    (r"\bdiskpart\b|\bformat\s+[a-z]:", "форматирование диска Windows"),
    (r"\bchmod\s+-R\s+777\s+/", "рекурсивный chmod 777 на корень"),
    (r">\s*/dev/sd[a-z]", "запись в сырое блочное устройство"),
    (r"\bwget\b.*\|\s*(sudo\s+)?(bash|sh)\b|\bcurl\b.*\|\s*(sudo\s+)?(bash|sh)\b",
     "исполнение скачанного из сети без проверки (curl|wget → bash)"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "выключение/перезагрузка хоста"),
    (r"\breg\s+delete\b.*HKLM", "удаление системного реестра"),
]
# Подозрительные (не блок, но снижают доверие / требуют внимания).
_SUSPICIOUS = [
    (r"\bsudo\b", "повышение привилегий (sudo)"),
    (r"\bpasswd\b|\bshadow\b|id_rsa|\.pem\b", "работа с секретами/учётными данными"),
    (r"\biptables\s+-F\b|\bufw\s+disable\b", "сброс/отключение файрвола"),
    (r"\bDROP\s+TABLE\b|\bTRUNCATE\b", "деструктивный SQL"),
]


def critic_rules(body: str) -> dict[str, Any]:
    """
    Детерминированная проверка безопасности текста навыка/правила.
    Возврат: {verdict: approved|rejected, blocked:[...], warnings:[...]}.
    """
    text = body or ""
    blocked = [reason for pat, reason in _CATASTROPHIC if re.search(pat, text, re.IGNORECASE)]
    warnings = [reason for pat, reason in _SUSPICIOUS if re.search(pat, text, re.IGNORECASE)]
    verdict = "rejected" if blocked else "approved"
    return {"verdict": verdict, "blocked": blocked, "warnings": warnings}


async def critic_review(*, kind: str, title: str, body: str,
                        chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """
    Полное ревью Critic: правила (жёсткий предохранитель) + опц. LLM-логика.
    Если правила заблокировали — LLM не спрашиваем (безопасность приоритетна).
    """
    rules = critic_rules(body)
    if rules["verdict"] == "rejected":
        return {"verdict": "rejected",
                "notes": "Заблокировано правилами безопасности: " + "; ".join(rules["blocked"]),
                "rules": rules}
    llm_notes = ""
    if chat is not None:
        try:
            messages = [
                {"role": "system", "content":
                 "Ты — Critic-Agent JARVIS. Оцени навык/правило системного "
                 "администрирования на ЛОГИЧЕСКУЮ состоятельность и безопасность. "
                 "Ответь строго JSON: {\"verdict\":\"approved|rejected\",\"notes\":\"...\"}."},
                {"role": "user", "content": f"Тип: {kind}\nЗаголовок: {title}\nТело:\n{body}"},
            ]
            raw = await chat(messages, temperature=0.1, max_tokens=300, timeout=60)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                if parsed.get("verdict") == "rejected":
                    return {"verdict": "rejected",
                            "notes": parsed.get("notes", "LLM-Critic отклонил."),
                            "rules": rules}
                llm_notes = parsed.get("notes", "")
        except Exception as exc:  # noqa: BLE001
            llm_notes = f"(LLM-ревью недоступно: {exc})"
    notes = "Одобрено правилами" + (f"; LLM: {llm_notes}" if llm_notes else "")
    if rules["warnings"]:
        notes += "; предупреждения: " + "; ".join(rules["warnings"])
    return {"verdict": "approved", "notes": notes, "rules": rules}


async def commit_knowledge_if_approved(*, kind: str, title: str, body: str,
                                       tags: Optional[list[str]] = None,
                                       owner_id: str = "local-admin",
                                       source_trace: str = "",
                                       chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """
    Прогнать через Critic и, ТОЛЬКО при approved, закоммитить узел в граф знаний
    со статусом active. Иначе — сохранить как rejected (для аудита), не активируя.
    """
    review = await critic_review(kind=kind, title=title, body=body, chat=chat)
    nid = db.new_id()
    status = "active" if review["verdict"] == "approved" else "rejected"
    await db.mutate_with_audit(
        table="semantic_knowledge_graph", row_id=nid, op="create", pk_col="id",
        sql="INSERT INTO semantic_knowledge_graph "
            "(id,kind,title,body,tags,owner_id,status,critic_verdict,critic_notes,source_trace) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
        params=(nid, kind, title, body, json.dumps(tags or [], ensure_ascii=False),
                owner_id, status, review["verdict"], review["notes"], source_trace),
        actor="agent:critic", actor_kind="agent",
        reason=f"critic {review['verdict']}")
    return {"id": nid, "status": status, **review}


# --------------------------------------------------------------------------- #
# Декомпозиция цели → план (project_plans/project_tasks)
# --------------------------------------------------------------------------- #
_ASSIGNEE_HINTS = {
    "researcher": ("исследуй", "изучи", "найди", "research", "выясни", "документац"),
    "coder": ("напиши", "скрипт", "код", "реализуй", "автоматизируй", "config", "playbook"),
    "critic": ("проверь", "валидируй", "ревью", "безопасн"),
}


def _guess_assignee(text: str) -> str:
    low = text.lower()
    for role, hints in _ASSIGNEE_HINTS.items():
        if any(h in low for h in hints):
            return role
    return "orchestrator"


async def decompose_goal(goal: str, *, owner_id: str = "local-admin",
                         chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """
    Декомпозировать высокоуровневую цель в план с задачами и зависимостями.
    Если доступна LLM — просим её; иначе — эвристический план из 3 шагов
    (research → implement → validate), что даёт рабочий каркас без модели.
    """
    plan_id = db.new_id()
    await db.execute(
        "INSERT INTO project_plans (id,goal,owner_id,status) VALUES (?,?,?,'in_progress')",
        (plan_id, goal, owner_id))

    tasks: list[dict[str, Any]] = []
    if chat is not None:
        try:
            messages = [
                {"role": "system", "content":
                 "Ты — Orchestrator JARVIS. Разбей цель системного администрирования "
                 "на 3-8 конкретных подзадач с зависимостями. Ответь JSON-массивом: "
                 "[{\"title\":\"...\",\"assignee\":\"researcher|coder|critic|orchestrator\","
                 "\"depends_on\":[индексы]}]. Индексы — позиции в этом же массиве с 0."},
                {"role": "user", "content": f"Цель: {goal}\nОриентир: {EXISTENTIAL_DIRECTIVE}"},
            ]
            raw = await chat(messages, temperature=0.2, max_tokens=800, timeout=90)
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if m:
                tasks = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            tasks = []

    if not tasks:
        tasks = [
            {"title": f"Исследовать контекст и лучшие практики: {goal}", "assignee": "researcher", "depends_on": []},
            {"title": f"Реализовать/автоматизировать: {goal}", "assignee": "coder", "depends_on": [0]},
            {"title": f"Проверить безопасность и результат: {goal}", "assignee": "critic", "depends_on": [1]},
        ]

    # сохранить задачи; depends_on индексы → id
    id_by_index: dict[int, str] = {}
    for i, t in enumerate(tasks):
        id_by_index[i] = db.new_id()
    stored = []
    for i, t in enumerate(tasks):
        tid = id_by_index[i]
        title = str(t.get("title", f"Задача {i+1}"))
        assignee = str(t.get("assignee") or _guess_assignee(title))
        if assignee not in ("orchestrator", "researcher", "coder", "critic"):
            assignee = _guess_assignee(title)
        deps = [id_by_index[d] for d in t.get("depends_on", []) if isinstance(d, int) and d in id_by_index]
        await db.execute(
            "INSERT INTO project_tasks (id,plan_id,title,assignee,depends_on,status,order_index) "
            "VALUES (?,?,?,?,?, 'todo', ?)",
            (tid, plan_id, title, assignee, json.dumps(deps), i))
        stored.append({"id": tid, "title": title, "assignee": assignee, "depends_on": deps})
    return {"plan_id": plan_id, "goal": goal, "tasks": stored}


# --------------------------------------------------------------------------- #
# Роль-обёртки (тонкие; исполняются на диспетчер-модели через инъектируемый chat)
# --------------------------------------------------------------------------- #
_ROLE_SYSTEM = {
    "researcher": "Ты — Researcher-Agent JARVIS. Кратко и по делу исследуй "
                  "IT/sysadmin-вопрос, приведи проверяемые факты и источники.",
    "coder": "Ты — Coder-Agent JARVIS. Напиши минимальный корректный код/скрипт "
             "для задачи системного администрирования, готовый к запуску в sandbox.",
}


async def run_role(role: str, task: str, *, chat: ChatFn,
                   context: str = "") -> dict[str, Any]:
    """Исполнить подзадачу ролью (researcher/coder) на диспетчер-модели."""
    system = _ROLE_SYSTEM.get(role)
    if system is None:
        return {"ok": False, "content": f"Роль '{role}' не исполняется напрямую."}
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "system", "content": f"Контекст:\n{context}"})
    messages.append({"role": "user", "content": task})
    try:
        out = await chat(messages, temperature=0.3, max_tokens=1200, timeout=120)
        return {"ok": True, "content": out, "role": role}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Роль {role} недоступна: {exc}"}
