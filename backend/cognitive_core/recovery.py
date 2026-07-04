# -*- coding: utf-8 -*-
"""
recovery.py — Recovery-Agent JARVIS OS: авто-диагностика и самолечение.

Задача: когда компонент падает (audio/vLLM/sandbox/bridge/docker) или в логах
traceback — проанализировать причину, предложить конкретную починку и, при
достаточном уровне автономии, применить её (с HITL для рискованного).

Двухслойно (как Critic):
    1. База правил (детерминирована, оффлайн, тестируема): сопоставляет
       сигнатуры traceback/ошибок с известными причинами и командами починки
       (нет OS-пакета → apt install; сервис лёг → docker restart; порт занят →
       найти и освободить; нет модуля Python → pip install…).
    2. LLM-диагностика (опц.) для незнакомых случаев.

Применение фикса проходит HITL по autonomy_level пользователя и danger_level
самого фикса: безопасные (install/restart) — при autonomy>=1; рискованные —
только с явным одобрением. Каждый шаг пишется в system_health_snapshots.

Исполнение команд инъектируется (runner) — тестируется без реального хоста.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Optional

from . import db, jsonx

Runner = Callable[[str], Awaitable[tuple[Optional[int], str]]]  # cmd -> (rc, out)
ChatFn = Callable[..., Awaitable[str]]

# База правил: (regex сигнатуры, причина, команда-починка, danger 0..3).
_RULES: list[tuple[str, str, str, int]] = [
    (r"No module named '([\w\.]+)'", "Отсутствует Python-модуль '{0}'",
     "pip install {0}", 1),
    (r"libsndfile|sndfile", "Нет системной библиотеки libsndfile (аудио)",
     "apt-get install -y libsndfile1", 1),
    (r"espeak", "Нет espeak-ng (TTS)", "apt-get install -y espeak-ng", 1),
    (r"portaudio|PortAudio", "Нет PortAudio (аудио-устройства)",
     "apt-get install -y libportaudio2", 1),
    (r"CUDA out of memory|OutOfMemoryError|out of memory",
     "OOM на GPU — не хватает VRAM для компонента",
     "python jarvis.py freevram  # затем снизить gpu_util/max_len или --no-audio", 2),
    (r"address already in use|port is already allocated|Errno 98",
     "Порт занят другим процессом",
     "docker compose -f wsl/docker-compose.agents.yml restart {svc}", 2),
    (r"Cannot connect to the Docker daemon|dockerDesktopLinuxEngine",
     "Docker-демон не отвечает",
     "python jarvis.py up  # лаунчер сам поднимет/полечит Docker", 1),
    (r"Wsl/Service/0x800703e3|wsl distro proxy .* exit",
     "WSL-интеграция Docker уронила движок",
     "python jarvis.py up  # авто-отключение WSL-интеграции + рестарт", 1),
    (r"unhealthy|Restarting \(\d+\)|exited with code",
     "Контейнер в рестарт-цикле", "docker logs --tail 50 {name}", 1),
    (r"Connection refused|ECONNREFUSED|Name or service not known",
     "Зависимый сервис недоступен (ещё не поднялся или упал)",
     "python jarvis.py diag", 0),
]


def diagnose_rules(text: str, *, component: str = "", name: str = "",
                   svc: str = "") -> dict[str, Any]:
    """
    Детерминированная диагностика по сигнатурам. Возврат:
    {matched:bool, cause, suggested_fix, danger, capture}.
    """
    for pattern, cause_t, fix_t, danger in _RULES:
        m = re.search(pattern, text or "", re.IGNORECASE)
        if m:
            arg = m.group(1) if m.groups() else ""
            cause = cause_t.format(arg) if "{0}" in cause_t else cause_t
            fix = (fix_t.format(arg) if "{0}" in fix_t else fix_t) \
                .replace("{svc}", svc or component or "backend") \
                .replace("{name}", name or f"jarvis-{component or 'backend'}")
            return {"matched": True, "cause": cause, "suggested_fix": fix,
                    "danger": danger, "capture": arg}
    return {"matched": False, "cause": "", "suggested_fix": "", "danger": 0}


async def diagnose(*, component: str, detail: str, name: str = "", svc: str = "",
                   chat: Optional[ChatFn] = None) -> dict[str, Any]:
    """
    Полная диагностика: правила → (если не сматчилось и есть LLM) LLM-анализ.
    Пишет снимок в system_health_snapshots.
    """
    rules = diagnose_rules(detail, component=component, name=name, svc=svc)
    result = dict(rules)
    if not rules["matched"] and chat is not None:
        try:
            raw = await chat([
                {"role": "system", "content":
                 "Ты — Recovery-Agent JARVIS. По логу/traceback определи причину и "
                 "предложи ОДНУ безопасную команду починки. Ответь JSON: "
                 "{\"cause\":\"...\",\"suggested_fix\":\"...\",\"danger\":0..3}."},
                {"role": "user", "content": f"Компонент: {component}\nЛог:\n{detail[:2000]}"}],
                temperature=0.1, max_tokens=300, timeout=60)
            parsed = jsonx.first_obj(raw)
            if parsed:
                result.update({"matched": True, "cause": parsed.get("cause", ""),
                               "suggested_fix": parsed.get("suggested_fix", ""),
                               "danger": int(parsed.get("danger", 1)), "source": "llm"})
        except Exception:  # noqa: BLE001
            pass

    status = "degraded" if result.get("matched") else "unknown"
    await db.execute(
        "INSERT INTO system_health_snapshots (component,status,detail,suggested_fix) "
        "VALUES (?,?,?,?)",
        (component, status, detail[:1000], result.get("suggested_fix") or None))
    result["component"] = component
    return result


def _fix_allowed(danger: int, autonomy_level: int) -> bool:
    """Разрешено ли АВТО-применение без HITL: autonomy должен покрывать danger."""
    return autonomy_level > danger


async def apply_fix(*, component: str, suggested_fix: str, danger: int,
                    autonomy_level: int = 1, approved: bool = False,
                    runner: Optional[Runner] = None) -> dict[str, Any]:
    """
    Применить фикс. Если danger не покрыт autonomy_level и нет явного approved —
    ВЕРНУТЬ requires_hitl (не исполнять). Иначе — выполнить через runner.
    """
    if not suggested_fix:
        return {"ok": False, "error": "Нет предложенной починки."}
    if not approved and not _fix_allowed(danger, autonomy_level):
        return {"ok": False, "requires_hitl": True, "danger": danger,
                "suggested_fix": suggested_fix,
                "message": f"Фикс danger={danger} требует подтверждения "
                           f"(autonomy_level={autonomy_level})."}
    if runner is None:
        return {"ok": False, "error": "Нет исполнителя команд (runner)."}
    rc, out = await runner(suggested_fix)
    ok = rc in (0, None)
    await db.execute(
        "INSERT INTO system_health_snapshots (component,status,detail,suggested_fix,fix_applied) "
        "VALUES (?,?,?,?,1)",
        (component, "healthy" if ok else "down",
         f"applied: {suggested_fix}\nrc={rc}\n{out[:800]}", suggested_fix))
    await db.execute(
        "INSERT INTO agent_achievements (id,title,detail,category,importance) VALUES (?,?,?,?,?)",
        (db.new_id(), f"{'🔧 Починка' if ok else '⚠ Попытка починки'}: {component}",
         suggested_fix[:200], "recovery", 0.5))
    return {"ok": ok, "rc": rc, "output": out[:800], "applied": suggested_fix}


async def health_report() -> dict[str, Any]:
    """Человекочитаемый сводный отчёт: последний статус по каждому компоненту."""
    rows = await db.query(
        "SELECT component, status, detail, suggested_fix, MAX(snapshot_at) AS ts "
        "FROM system_health_snapshots GROUP BY component ORDER BY component")
    unhealthy = [r for r in rows if r["status"] in ("degraded", "down")]
    return {"components": rows, "healthy": not unhealthy,
            "unhealthy": [r["component"] for r in unhealthy]}
