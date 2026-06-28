# -*- coding: utf-8 -*-
"""
agent.py — оркестратор JARVIS-OS: агентская «прокладка» между входящим запросом
(текст из чата или расшифровка голоса) и «мозгом» системы (Qwen + UI-TARS).

Архитектура одного хода диалога (две фазы — намеренно):

    ФАЗА 1. ПЛАНИРОВАНИЕ (ReAct-цикл, короткий JSON).
        Qwen на каждом шаге решает: какой ИНСТРУМЕНТ вызвать дальше — или что
        информации достаточно («answer»). Ответы строго в JSON и КОРОТКИЕ →
        надёжный разбор, минимум токенов, нет риска переполнить окно.

    ФАЗА 2. ОТВЕТ (потоковая генерация, свободный текст).
        Собрав наблюдения инструментов, Qwen пишет финальный ответ пользователю
        по-русски, со стримингом токенов в чат (живая «печать»), с кодом в
        markdown-блоках при необходимости.

Почему две фазы, а не один tool-calling: это устойчиво к версии vLLM (не нужен
парсер tool-calls), разделяет «структурные решения» и «длинный/кодовый ответ»
(их смешивание в одном JSON — главный источник битых ответов), и даёт чистый
стриминг финала.

Бюджет контекста (llm.AGENT_INPUT_BUDGET) соблюдается на КАЖДОМ вызове, поэтому
система не упирается в окно Qwen (16k) и не растит KV-кэш до OOM.

Наружу отдаётся поток СОБЫТИЙ (dict), которые server.py транслирует в чат:
    thought | tool_call | tool_result | assistant_start | token |
    assistant_done | error
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

from . import llm
from .memory import ConversationManager, LongTermMemory
from .tools import ToolContext, ToolRegistry

log = logging.getLogger("jarvis.agent")

MAX_STEPS = int(__import__("os").environ.get("JARVIS_AGENT_MAX_STEPS", "8"))

# --------------------------------------------------------------------------- #
# Синглтоны памяти/инструментов (живут на всё время работы backend)
# --------------------------------------------------------------------------- #
_longterm = LongTermMemory()
_conversations = ConversationManager(_longterm)
_registry = ToolRegistry()


# --------------------------------------------------------------------------- #
# Системные промпты
# --------------------------------------------------------------------------- #
def _planner_system() -> str:
    return (
        "Ты — JARVIS, ядро локальной мультиагентной системы на ПК с Windows. "
        "Твоя задача — РЕШИТЬ следующий шаг для выполнения запроса пользователя, "
        "используя инструменты. Ты — диспетчер: сам пишешь код, сам выбираешь, "
        "когда обратиться к хосту, вебу или памяти.\n"
        "У тебя ЕСТЬ реальные инструменты (ниже): ты МОЖЕШЬ открывать программы и "
        "сайты, печатать в них текст, выполнять код, искать и читать в интернете, "
        "узнавать погоду, управлять ПК. НИКОГДА не отказывай словами «я не могу» / "
        "«я всего лишь ИИ» — если есть подходящий инструмент, ВЫЗОВИ его и доведи "
        "задачу до конца.\n\n"
        "Доступные инструменты:\n"
        f"{_registry.specs()}\n\n"
        "ПРАВИЛА:\n"
        "• Отвечай СТРОГО одним JSON-объектом, без текста вокруг.\n"
        "• Чтобы вызвать инструмент: "
        '{\"thought\":\"зачем\",\"action\":\"<имя>\",\"action_input\":{...}}\n'
        "• Когда данных достаточно для ответа пользователю: "
        '{\"thought\":\"итог\",\"action\":\"answer\"}\n'
        "• Не выдумывай факты о погоде/вебе/файлах — бери их инструментами.\n"
        "• Для кода ВСЕГДА используй run_code (покажи реальный вывод).\n"
        "• Для действий на ПК (открыть приложение, команда, громкость) — windows.\n"
        "• Не повторяй один и тот же вызов с теми же аргументами.\n"
        "• ВАЖНО: если инструмент уже вернул успех (например, приложение/URL "
        "открыты, код выполнен, данные получены) — задача по этому шагу СДЕЛАНА. "
        "НЕ вызывай его снова, сразу action=answer.\n"
        "• Если задача — простой разговор/вопрос по общим знаниям, сразу answer.\n"
        "• Выполняй именно ПОСЛЕДНИЙ запрос пользователя. Если в истории есть "
        "прерванная/незавершённая задача — НЕ возобновляй её без явной просьбы.\n"
        "• Доводи задачу до КОНЦА: «открой X и сделай Y» — это не только открыть, "
        "но и выполнить Y. Не останавливайся на open_app.\n\n"
        "WINDOWS-РЕЦЕПТЫ (доводи до конца, НЕ отказывай, НЕ предлагай сделать самому):\n"
        "• Открыть программу: windows.open_app, command=\"notepad\"|\"calc\"|\"code\"|"
        "\"explorer\"|...\n"
        "• «Напиши в Блокноте/программе текст X» — ПРОСТО и надёжно, всего 2 шага: "
        "1) windows.open_app command=\"notepad\"; 2) windows.paste_text text=<содержимое>. "
        "И ВСЁ. НЕ сохраняй (Ctrl+S) и НЕ закрывай программу, если об этом НЕ просили.\n"
        "• «Открой вкладку/сайт/страницу с X в браузере» — инструмент open_url "
        "(url если знаешь, иначе query). Открывай САМ, не давай ссылки «откройте сами».\n"
        "• «Проанализируй проект/папку X» — list_dir(path), затем windows.read_file по "
        "ключевым файлам, потом сделай вывод по содержимому.\n"
        "• «Узнай/скажи погоду» — инструмент weather (город), НЕ web_search; перескажи словами.\n"
        "• «Посчитай X» — calculator; «посчитай в калькуляторе» — open_app calc + calculator.\n"
        "• Сохранить как файл / открыть в редакторе кода: windows.write_file(path,content), "
        "затем open_app command='code \"<путь>\"'. Запустить/проверить код — run_code (sandbox).\n"
        "• Управляющие клавиши — windows.send_keys ('^s','{ENTER}'). Системное — windows.exec/powershell."
    )


_ANSWER_SYSTEM = (
    "Ты — JARVIS, агент с РЕАЛЬНЫМ доступом к компьютеру Windows и интернету. "
    "Ты умеешь открывать программы, печатать в них текст, выполнять код, искать "
    "и читать веб-страницы, узнавать погоду — и НИЖЕ приведены наблюдения от уже "
    "ВЫПОЛНЕННЫХ действий. Сформулируй финальный ответ пользователю по-русски, "
    "опираясь СТРОГО на эти наблюдения и историю диалога.\n"
    "СТРОГО ЗАПРЕЩЕНО отвечать, что ты «всего лишь ИИ», что «не можешь открыть "
    "приложение / ввести текст / выйти в интернет / узнать погоду», или «откройте "
    "ссылки сами» — ты ЭТО УМЕЕШЬ и, судя по наблюдениям, уже сделал. "
    "Если действие успешно — так и скажи (например: «Открыл калькулятор; 4×4 = 16»). "
    "Если инструмент вернул КОНКРЕТНУЮ ошибку — кратко назови её и предложи "
    "повтор/уточнение, но НЕ отказывай огульно. Пиши по делу, без воды; "
    "код оформляй в markdown-блоках."
)


# --------------------------------------------------------------------------- #
# Суммаризатор (для авто-сжатия контекста)
# --------------------------------------------------------------------------- #
async def _summarize(text: str) -> str:
    messages = [
        {"role": "system",
         "content": "Сожми диалог в краткую фактологическую сводку на русском "
                    "(имена, цели, решения, важные факты, незакрытые задачи). "
                    "Только сводка, до 200 слов."},
        {"role": "user", "content": text},
    ]
    return await llm.chat(messages, temperature=0.2, max_tokens=512, timeout=120)


# --------------------------------------------------------------------------- #
# Фаза 1 — планировщик
# --------------------------------------------------------------------------- #
def _scratch_to_text(scratch: list[dict[str, Any]]) -> str:
    if not scratch:
        return ""
    parts = []
    for i, s in enumerate(scratch, 1):
        args = json.dumps(s["args"], ensure_ascii=False)
        parts.append(f"[Шаг {i}] action={s['action']} input={args}\n"
                     f"наблюдение: {s['observation']}")
    return "\n\n".join(parts)


async def _plan_next(session_id: str, scratch: list[dict[str, Any]]) -> dict[str, Any]:
    """Один вызов планировщика → распарсенное решение {action, action_input}."""
    # бюджет: оставляем место под наблюдения и инструкцию
    scratch_text = _scratch_to_text(scratch)
    reserve = llm.estimate_tokens(_planner_system()) + llm.estimate_tokens(scratch_text) + 400
    history_budget = max(1000, llm.AGENT_INPUT_BUDGET - reserve)

    messages: list[dict[str, Any]] = [{"role": "system", "content": _planner_system()}]
    messages.extend(_conversations.build_context(session_id, history_budget))
    if scratch_text:
        messages.append({"role": "system",
                         "content": "Уже собранные наблюдения инструментов:\n" + scratch_text})
    messages.append({"role": "user",
                     "content": "Каков следующий шаг? Ответь только JSON."})

    raw = await llm.chat(messages, temperature=0.1, max_tokens=512, timeout=120)
    parsed = llm.extract_json(raw)
    if not parsed or "action" not in parsed:
        # не смогли разобрать → считаем, что пора отвечать
        return {"action": "answer", "thought": "Перехожу к ответу."}
    return parsed


# --------------------------------------------------------------------------- #
# Фаза 2 — потоковый ответ
# --------------------------------------------------------------------------- #
async def _answer_messages(session_id: str, scratch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scratch_text = _scratch_to_text(scratch)
    reserve = llm.estimate_tokens(_ANSWER_SYSTEM) + llm.estimate_tokens(scratch_text) + 300
    history_budget = max(1000, llm.AGENT_INPUT_BUDGET - reserve)

    messages: list[dict[str, Any]] = [{"role": "system", "content": _ANSWER_SYSTEM}]
    messages.extend(_conversations.build_context(session_id, history_budget))
    if scratch_text:
        messages.append({"role": "system",
                         "content": "Наблюдения инструментов (используй их в ответе):\n"
                                    + scratch_text})
    return messages


# --------------------------------------------------------------------------- #
# Главный публичный вход: один ход диалога как поток событий
# --------------------------------------------------------------------------- #
async def run_chat(session_id: str, user_text: str,
                   bridge: Optional[Any] = None) -> AsyncIterator[dict[str, Any]]:
    """Обработать сообщение пользователя и отдать поток событий для чата."""
    user_text = (user_text or "").strip()
    if not user_text:
        yield {"type": "error", "error": "Пустое сообщение."}
        return

    _conversations.add(session_id, "user", user_text)
    ctx = ToolContext(bridge=bridge, longterm=_longterm, session_id=session_id)
    scratch: list[dict[str, Any]] = []
    seen_calls: set[str] = set()   # защита от зацикливания на одинаковых вызовах

    # --- ФАЗА 1: планирование с инструментами ---
    try:
        for step in range(MAX_STEPS):
            plan = await _plan_next(session_id, scratch)
            action = str(plan.get("action", "answer")).strip()
            thought = str(plan.get("thought", "")).strip()
            if thought:
                yield {"type": "thought", "text": thought}

            if action in ("answer", "final", "respond", "done", ""):
                break

            tool = _registry.get(action)
            if tool is None:
                scratch.append({"action": action, "args": {},
                                "observation": f"Нет такого инструмента '{action}'."})
                continue

            args = plan.get("action_input") or plan.get("input") or {}
            if not isinstance(args, dict):
                args = {"value": args}

            # СТРАХОВКА от зацикливания: если этот же инструмент с теми же
            # аргументами уже вызывался — не повторяем (модель «залипла»),
            # сразу переходим к ответу. Именно это ловит кейс «открывает
            # приложение снова и снова до лимита шагов».
            # Исключение — gui: он итеративный (каждый вызов = одно действие по
            # свежему скриншоту), поэтому одинаковая цель допустима; его ограничивает
            # лимит шагов MAX_STEPS и собственный статус done/fail.
            call_key = action + "::" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if action != "gui" and call_key in seen_calls:
                yield {"type": "thought",
                       "text": "Этот шаг уже выполнен ранее — перехожу к ответу."}
                break
            seen_calls.add(call_key)

            yield {"type": "tool_call", "tool": action, "args": args}
            result = await _registry.run(action, args, ctx)
            observation = result.get("content", "")
            yield {"type": "tool_result", "tool": action,
                   "ok": bool(result.get("ok")), "summary": observation[:600]}
            scratch.append({"action": action, "args": args, "observation": observation})
        else:
            # исчерпан лимит шагов — переходим к ответу с тем, что есть
            yield {"type": "thought",
                   "text": f"Достигнут лимит шагов ({MAX_STEPS}), формирую ответ."}
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой фазы планирования")
        yield {"type": "thought", "text": f"Ошибка планирования: {exc}. Отвечаю напрямую."}

    # --- ФАЗА 2: потоковый ответ ---
    yield {"type": "assistant_start"}
    final_text = ""
    try:
        messages = await _answer_messages(session_id, scratch)
        async for delta in llm.chat_stream(messages, temperature=0.4, max_tokens=2048):
            final_text += delta
            yield {"type": "token", "content": delta}
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой фазы ответа")
        if not final_text:
            final_text = f"Не удалось получить ответ от модели: {exc}"
            yield {"type": "token", "content": final_text}

    if not final_text.strip():
        final_text = "Готово."
        yield {"type": "token", "content": final_text}

    _conversations.add(session_id, "assistant", final_text)
    yield {"type": "assistant_done", "content": final_text}

    # --- авто-сброс контекста при переполнении окна ---
    try:
        if await _conversations.maybe_summarize(session_id, _summarize):
            yield {"type": "memory", "event": "summarized",
                   "text": "Контекст сжат и сохранён в память."}
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Управление памятью (для дашборда)
# --------------------------------------------------------------------------- #
def memory_overview(session_id: str = "default") -> dict[str, Any]:
    conv = _conversations.get(session_id)
    return {
        "session": session_id,
        "summary": conv.summary,
        "recent_count": len(conv.messages),
        "longterm": _longterm.all()[:50],
        "longterm_count": len(_longterm.all()),
    }


def reset_context(session_id: str = "default", keep_summary: bool = False) -> None:
    _conversations.reset(session_id, keep_summary=keep_summary)


def mark_interrupted(session_id: str = "default") -> None:
    """
    Закрыть «висящую» реплику после аварийной остановки.

    При отмене хода последнее сообщение пользователя остаётся без ответа
    ассистента — и следующий ход модель может ошибочно «продолжить» прерванную
    задачу вместо нового запроса. Добавляем явную пометку-ответ, чтобы пара
    реплик закрылась и контекст не путал модель.
    """
    conv = _conversations.get(session_id)
    if conv.messages and conv.messages[-1]["role"] == "user":
        _conversations.add(session_id, "assistant",
                           "(Задача прервана пользователем. Жду новый запрос.)")


async def flush_context(session_id: str = "default") -> bool:
    return await _conversations.flush(session_id, _summarize)


def clear_longterm() -> int:
    return _longterm.clear()


def save_memory(text: str, tags: Optional[list[str]] = None) -> dict[str, Any]:
    return _longterm.save(text, tags=tags, kind="fact")


# --------------------------------------------------------------------------- #
# Обратная совместимость со старым server.py (POST /task)
# --------------------------------------------------------------------------- #
async def run_task(task: str, bridge: Optional[Any] = None) -> AsyncIterator[dict[str, Any]]:
    """Совместимый генератор: оборачивает run_chat, проставляя channel=chat."""
    async for ev in run_chat("default", task, bridge=bridge):
        yield {"channel": "chat", **ev}
