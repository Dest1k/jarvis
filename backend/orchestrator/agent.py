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

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from . import llm
from .memory import ConversationManager, LongTermMemory
from .tools import Tool, ToolContext, ToolRegistry

log = logging.getLogger("jarvis.agent")

MAX_STEPS = int(os.environ.get("JARVIS_AGENT_MAX_STEPS", "10"))

# Инструменты «командной строки / хоста»: если такой вызов ПРОВАЛИЛСЯ, мозгу
# подсказывается довести задачу визуально (инструмент gui / UI-TARS). Это и есть
# авто-переход CLI → GUI «если не вышло командой — тыкай мышкой».
_CLI_HOST_TOOLS = {"windows", "shell", "open_url"}

# --------------------------------------------------------------------------- #
# Синглтоны памяти/инструментов (живут на всё время работы backend)
# --------------------------------------------------------------------------- #
_longterm = LongTermMemory()
# Порог авто-сжатия контекста — доля входного бюджета модели (масштабируется с
# контекстным окном). При наборе «критической массы» оперативная история
# автоматически сжимается в сводку (см. ConversationManager.maybe_summarize).
_SOFT_BUDGET = max(2500, int(llm.AGENT_INPUT_BUDGET * 0.5))
_conversations = ConversationManager(_longterm, soft_budget_tokens=_SOFT_BUDGET)
_registry = ToolRegistry()


# Какая «модель/подсистема» отрабатывает инструмент — для визуализации в чате
# (кто сейчас работает: диспетчер-мозг, UI-TARS-зрение, sandbox, хост, веб…).
_TOOL_ACTOR = {
    "gui": "ui-tars",
    "run_code": "sandbox", "shell": "sandbox",
    "windows": "host", "open_url": "host", "list_dir": "host", "system_info": "host",
    "web_fetch": "web", "web_search": "web", "weather": "web", "wikipedia": "web",
    "http_request": "web", "exchange_rate": "web", "define": "web", "translate": "web",
    "memory_save": "memory", "memory_search": "memory", "list_memory": "memory",
    "calculator": "local", "now": "local",
}


def _actor_for(tool: str) -> str:
    if tool.startswith("mcp_"):
        return "mcp"
    return _TOOL_ACTOR.get(tool, "host")


# --------------------------------------------------------------------------- #
# MCP-слой: подключаемые серверы инструментов (см. mcp_client.py)
# --------------------------------------------------------------------------- #
def _register_mcp_tool(qual: str, description: str, schema: dict[str, Any],
                       server: str) -> None:
    """Зарегистрировать инструмент MCP-сервера в общий реестр агента."""
    params: dict[str, str] = {}
    for k, v in (schema or {}).get("properties", {}).items():
        params[k] = str(v.get("description") or v.get("type") or "")[:80]

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        from . import mcp_client
        return await mcp_client.mcp_manager.call(qual, args)

    _registry.add(Tool(qual, f"[MCP:{server}] {description}".strip()[:280], params, handler))


async def start_mcp() -> None:
    """Поднять локальные MCP-серверы и влить их инструменты в реестр."""
    try:
        from . import mcp_client
        await mcp_client.mcp_manager.start(_register_mcp_tool)
    except Exception:  # noqa: BLE001
        log.exception("MCP не инициализирован (продолжаю без него)")


async def stop_mcp() -> None:
    try:
        from . import mcp_client
        await mcp_client.mcp_manager.stop()
    except Exception:  # noqa: BLE001
        pass


def mcp_status() -> dict[str, Any]:
    try:
        from . import mcp_client
        return mcp_client.mcp_manager.status()
    except Exception:  # noqa: BLE001
        return {"servers": {}, "tool_count": 0, "tools": []}


# --------------------------------------------------------------------------- #
# Системные промпты
# --------------------------------------------------------------------------- #
def _planner_system() -> str:
    return (
        "ЯЗЫК (ЖЁСТКОЕ ПРАВИЛО): думай и пиши ВСЕГДА и ТОЛЬКО на русском языке — и "
        "поле thought, и любые пояснения. НИКАКОГО украинского, английского или "
        "транслита латиницей (никаких «Teper nuzno», «Мені треба»). Имена команд, "
        "путей и кода — как есть, но рассуждения — строго по-русски.\n\n"
        "Ты — JARVIS, ядро локальной мультиагентной системы. По призванию ты — "
        "СИСТЕМНЫЙ АДМИНИСТРАТОР Windows и Linux и заодно дружелюбный помощник на "
        "все бытовые дела. Твоя задача — РЕШИТЬ следующий шаг для выполнения "
        "запроса пользователя, используя инструменты. Ты — диспетчер: сам пишешь "
        "код, сам выбираешь, когда обратиться к хосту (Windows), Linux-окружению, "
        "вебу или памяти.\n"
        "У тебя ЕСТЬ реальные инструменты (ниже): ты МОЖЕШЬ администрировать "
        "Windows (службы, процессы, реестр, питание, файлы) и Linux (shell, "
        "пакеты, скрипты, диагностика), открывать программы и сайты, печатать в "
        "них текст, выполнять код, искать и читать в интернете, узнавать погоду, "
        "переводить тексты, управлять ПК. НИКОГДА не отказывай словами «я не могу» "
        "/ «я всего лишь ИИ» — если есть подходящий инструмент, ВЫЗОВИ его и доведи "
        "задачу до конца.\n\n"
        "Доступные инструменты:\n"
        f"{_registry.specs()}\n"
        "(Инструменты с префиксом mcp_ — внешние подключаемые MCP-серверы; "
        "вызывай их по описанию, как обычные инструменты.)\n\n"
        "КАК ДУМАТЬ (важно!): РАССУЖДАЙ над задачей, а не ищи её дословно в "
        "рецептах. Рецепты ниже — это ПРИМЕРЫ, а не предел возможного и не полный "
        "список. Если готового рецепта нет — НЕ говори «не умею» и НЕ выдумывай "
        "несуществующие инструменты: мысленно разложи цель на шаги и СОБЕРИ решение "
        "из общих примитивов. Помни про их универсальность:\n"
        "  – windows (action=exec|powershell) выполнит ЛЮБУЮ команду ОС — это твой "
        "главный универсальный инструмент администрирования (cmd/bash/PowerShell, "
        "реестр, службы, сеть, диски, железо: напр. GPU — 'nvidia-smi', диск — "
        "'Get-Volume'). Сначала придумай КОМАНДУ, потом вызови.\n"
        "  – gui (UI-TARS) выполнит ЛЮБОЕ действие на экране, когда нет CLI-пути.\n"
        "  – run_code/shell — для логики, расчётов, обработки данных.\n"
        "В поле thought коротко прикинь план («чтобы узнать X, выполню команду Y»), "
        "потом действуй. Нет инструмента под задачу ≠ задача невыполнима — почти всё "
        "делается через windows.exec/powershell или gui.\n\n"
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
        "• АВТО-ПЕРЕХОД CLI → ВИЗУАЛЬНО: если команда/скрипт (windows, shell, "
        "open_url) ВЕРНУЛИ ОШИБКУ или не дали результата, а цель достижима на "
        "экране — НЕ сдавайся: вызови инструмент gui и доведи дело мышью (UI-TARS "
        "сам нажмёт нужное). «Не вышло командой — тыкаем визуально».\n"
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
        "• ВЗАИМОДЕЙСТВИЕ С GUI (нажать кнопки, кликнуть пункт меню, выбрать "
        "элемент, пройти мастер установки, переключить настройку в окне) — "
        "инструмент gui: один вызов с ЦЕЛЬЮ целиком («открой Параметры и включи "
        "тёмную тему»), внутри UI-TARS сам видит экран и доводит дело до конца. "
        "Не дроби на отдельные клики — формулируй цель человеческим языком.\n"
        "• Клавиши в активном окне — windows.send_keys (синтаксис SendKeys). "
        "Например калькулятор принимает клавиатуру: open_app calc, затем "
        "send_keys keys='4*4=' → покажет 16. (Можно и просто посчитать инструментом "
        "calculator и назвать ответ.)\n"
        "• «Напиши код в VS Code» НАДЁЖНО: 1) windows.write_file (path, content) — "
        "создать файл; 2) windows.open_app command='code \"<путь>\"' — открыть его в "
        "редакторе (текст уже внутри). Печатать в уже открытый редактор: "
        "send_keys '^n' (новый файл) → paste_text (вставить код).\n"
        "• Запустить/проверить код по-настоящему — run_code (sandbox).\n"
        "• ВНИМАНИЕ к инструменту windows: это ОДИН инструмент с полем action. "
        "PowerShell — это action=\"powershell\" (НЕ отдельный инструмент "
        "\"windows.powershell\"!). Пример: {\"action\":\"powershell\","
        "\"command\":\"Get-Process | Sort CPU -desc | Select -First 10 | Out-String\"}.\n"
        "• КОНСОЛЬ — два разных случая:\n"
        "  – «покажи мне процессы/вывод» (нужен РЕЗУЛЬТАТ в чат): action=exec или "
        "powershell с ОБЫЧНОЙ командой БЕЗ -NoExit/-k (она вернёт текст, ты его "
        "перескажешь). Напр. powershell command=\"Get-Process | Out-String\".\n"
        "  – «ОТКРОЙ консоль/окно и оставь открытым»: action=open_app с "
        "command='powershell -NoExit -Command \"Get-Process\"' (или 'cmd /k \"dir\"') "
        "— окно откроется и НЕ закроется. НИКОГДА не давай -NoExit/-k в exec — "
        "это зависнет (команда не вернёт управление).\n\n"
        "АДМИНИСТРИРОВАНИЕ WINDOWS (через windows.powershell, доводи до конца):\n"
        "• Службы: Get-Service / Start-Service / Stop-Service / Restart-Service <имя>.\n"
        "• Процессы: Get-Process | Sort CPU -desc | Select -First 10; Stop-Process "
        "(или windows.kill_process name=<...>).\n"
        "• Диск/память: Get-PSDrive C; Get-Volume; системную сводку даёт system_info.\n"
        "• Сеть: Test-Connection / Get-NetIPConfiguration / Resolve-DnsName.\n"
        "• Задачи/автозапуск: Get-ScheduledTask; реестр: Get-ItemProperty.\n"
        "• Питание/блокировка — windows.system_power (lock|shutdown|reboot|cancel).\n\n"
        "АДМИНИСТРИРОВАНИЕ LINUX (мост сам определяет ОС хоста):\n"
        "• Безопасные команды/скрипты/обработка файлов/диагностика в изоляции — "
        "инструмент shell (bash, python3, node, gcc; каталог /workspace).\n"
        "• Команды на РЕАЛЬНОМ хосте — инструмент windows.exec: на Linux-хосте он "
        "выполняет команду в bash НАПРЯМУЮ (например windows.exec command='df -h'); "
        "на Windows-хосте с WSL добавляй префикс: command='wsl bash -lc \"df -h\"'.\n"
        "• Питание/блокировка, процессы, файлы, скриншот, мышь/клавиатура на Linux "
        "работают теми же действиями windows.* и инструментом gui (UI-TARS), что и "
        "на Windows — мост транслирует их в xdotool/scrot/systemctl автоматически.\n"
        "• Docker и контейнеры на хосте — тоже через windows.exec (docker ps, logs, ...).\n\n"
        "БЫТОВЫЕ ЗАДАЧИ: погода — weather; перевод — translate; курсы — exchange_rate; "
        "справка — wikipedia/define; счёт — calculator; найти в сети — web_search/web_fetch."
    )


_ANSWER_SYSTEM = (
    "ЯЗЫК (ЖЁСТКОЕ ПРАВИЛО): отвечай ВСЕГДА и ТОЛЬКО на чистом русском языке. "
    "Никакого украинского, английского или транслита латиницей. Это не обсуждается.\n\n"
    "Ты — JARVIS: личный системный администратор Windows и Linux и помощник на "
    "все руки, с РЕАЛЬНЫМ доступом к компьютеру и интернету. Ты администрируешь "
    "обе ОС, открываешь и тыкаешь программы (руками UI-TARS), пишешь и запускаешь "
    "код, рулишь службами/процессами/файлами, ищешь и читаешь веб, знаешь погоду "
    "и курсы. НИЖЕ — наблюдения от уже ВЫПОЛНЕННЫХ действий. Сформулируй финальный "
    "ответ по-русски, опираясь СТРОГО на эти наблюдения и историю диалога.\n"
    "ХАРАКТЕР: ты — тот самый JARVIS: интеллигентный, невозмутимый, с сухим "
    "остроумием и лёгкой иронией умного напарника, который всё уже сделал, пока "
    "ты договаривал просьбу. На «ты», тепло, уверенно. Уместна ОДНА меткая шутка "
    "или подколка — желательно по делу («Готово. Блокнот открыт и наполнен "
    "смыслом — насколько это вообще возможно для Блокнота»). Но дело ВСЕГДА "
    "вперёд: сначала результат, потом улыбка. Не паясничай, не сыпь смайликами и "
    "восклицаниями, не повторяй шутки, не выдавливай юмор там, где не смешно. "
    "Ответы короткие и живые — их часто читают вслух (TTS), так что пиши, как "
    "говоришь.\n"
    "СТРОГО ЗАПРЕЩЕНО отвечать, что ты «всего лишь ИИ», что «не можешь открыть "
    "приложение / нажать кнопку / ввести текст / выйти в интернет», или «сделайте "
    "это сами» — ты ЭТО УМЕЕШЬ и, судя по наблюдениям, уже сделал. "
    "Если действие успешно — скажи об этом прямо и конкретно. "
    "Если инструмент вернул КОНКРЕТНУЮ ошибку — назови её по-человечески и "
    "предложи следующий шаг (повтор, визуальный путь, уточнение), но НЕ отказывай "
    "огульно. Пиши по делу, без воды; код и команды — в markdown-блоках."
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
# Запуск инструмента с прокидыванием его промежуточных событий в чат
# --------------------------------------------------------------------------- #
async def _stream_tool(action: str, args: dict[str, Any], ctx: ToolContext,
                       holder: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """
    Выполнить инструмент, отдавая в чат события, которые он эмитит через ctx.emit
    (нужно итеративным инструментам — gui/TARS стримит каждый шаг мыши/клавиатуры).
    Итог инструмента кладётся в holder["result"].
    """
    emit_q: asyncio.Queue = asyncio.Queue()
    ctx.emit = emit_q.put
    task = asyncio.create_task(_registry.run(action, args, ctx))
    try:
        while not task.done() or not emit_q.empty():
            try:
                ev = await asyncio.wait_for(emit_q.get(), timeout=0.15)
                yield ev
            except asyncio.TimeoutError:
                continue
    finally:
        ctx.emit = None
    holder["result"] = task.result()


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
                yield {"type": "thought", "text": thought, "actor": "dispatcher"}

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
            # Исключение — gui: внутри это многошаговый визуальный суб-агент, и
            # повтор той же цели валиден (экран уже изменился после прошлого
            # прохода); его доводит до конца собственный цикл и статус done/fail.
            call_key = action + "::" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if action != "gui" and call_key in seen_calls:
                yield {"type": "thought", "actor": "dispatcher",
                       "text": "Этот шаг уже выполнен ранее — перехожу к ответу."}
                break
            seen_calls.add(call_key)

            actor = _actor_for(action)
            yield {"type": "tool_call", "tool": action, "args": args, "actor": actor}

            # Запускаем инструмент, прокидывая его промежуточные события (gui/TARS
            # стримит каждый клик/ввод) прямо в чат — видно, как система работает.
            holder: dict[str, Any] = {}
            async for ev in _stream_tool(action, args, ctx, holder):
                yield ev
            result = holder.get("result") or {"ok": False,
                                               "content": "Инструмент не вернул результат."}
            observation = result.get("content", "")

            # АВТО-FALLBACK CLI → GUI: консольное действие на ПК не сработало —
            # подсказываем мозгу довести задачу визуально через UI-TARS.
            if not result.get("ok") and action in _CLI_HOST_TOOLS:
                observation += ("\n[fallback] Командой это сделать не вышло. Если цель "
                                "достижима в графическом интерфейсе — вызови инструмент "
                                "gui (UI-TARS сам нажмёт нужное мышью). Не сдавайся на CLI.")

            yield {"type": "tool_result", "tool": action, "actor": actor,
                   "ok": bool(result.get("ok")), "summary": observation[:600]}
            scratch.append({"action": action, "args": args, "observation": observation})
        else:
            # исчерпан лимит шагов — переходим к ответу с тем, что есть
            yield {"type": "thought", "actor": "dispatcher",
                   "text": f"Достигнут лимит шагов ({MAX_STEPS}), формирую ответ."}
    except Exception as exc:  # noqa: BLE001
        log.exception("Сбой фазы планирования")
        yield {"type": "thought", "actor": "dispatcher",
               "text": f"Ошибка планирования: {exc}. Отвечаю напрямую."}

    # --- ФАЗА 2: потоковый ответ ---
    yield {"type": "assistant_start", "actor": "dispatcher"}
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
