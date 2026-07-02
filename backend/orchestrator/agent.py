# -*- coding: utf-8 -*-
"""
agent.py — оркестратор JARVIS-OS: агентское ядро между входящим запросом
(текст из чата или расшифровка голоса) и «мозгом» системы.

Мозг здесь — ПОЛНОЦЕННЫЙ системный администратор Windows и Linux и бытовой
помощник: он сам выбирает канал исполнения (команда ОС / код / GUI-мышь /
веб / память), сам диагностирует ошибки и меняет подход, сам ПРОВЕРЯЕТ
результат административных изменений, прежде чем отчитаться.

Архитектура одного хода диалога (две фазы — намеренно):

    ФАЗА 1. ПЛАНИРОВАНИЕ (ReAct-цикл, короткий JSON).
        Мозг на каждом шаге решает: какой ИНСТРУМЕНТ вызвать дальше — или что
        информации достаточно («answer»). Ответы строго в JSON и КОРОТКИЕ →
        надёжный разбор, минимум токенов, нет риска переполнить окно.

    ФАЗА 2. ОТВЕТ (потоковая генерация, свободный текст).
        Собрав наблюдения инструментов, мозг пишет финальный ответ пользователю
        по-русски, со стримингом токенов в чат (живая «печать»), с кодом в
        markdown-блоках при необходимости.

Почему две фазы, а не один tool-calling: это устойчиво к версии vLLM (не нужен
парсер tool-calls), разделяет «структурные решения» и «длинный/кодовый ответ»
(их смешивание в одном JSON — главный источник битых ответов), и даёт чистый
стриминг финала.

Надёжность цикла (выучено на реальных сбоях локальных моделей):
    • нормализация действий — модель может назвать инструментом "powershell"
      или "windows.exec"; это молча превращается в правильный вызов windows;
    • защита от зацикливания — повтор того же вызова с теми же аргументами
      прерывается (кроме итеративных gui/see_screen: экран между вызовами
      меняется, повтор валиден);
    • эскалация при сбоях — две неудачи подряд у инструмента → мозгу
      подсказывается сменить подход (другая команда / другой канал / GUI);
    • авто-fallback CLI → GUI — «не вышло командой — тыкаем визуально».

Бюджет контекста (llm.AGENT_INPUT_BUDGET) соблюдается на КАЖДОМ вызове, поэтому
система не упирается в окно модели и не растит KV-кэш до OOM.

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

# Административные цепочки «изменить → проверить» длиннее простых вопросов,
# поэтому лимит шагов с запасом (переопределяется env).
MAX_STEPS = int(os.environ.get("JARVIS_AGENT_MAX_STEPS", "12"))

# Инструменты «командной строки / хоста»: если такой вызов ПРОВАЛИЛСЯ, мозгу
# подсказывается довести задачу визуально (инструмент gui). Это и есть
# авто-переход CLI → GUI «если не вышло командой — тыкай мышкой».
_CLI_HOST_TOOLS = {"windows", "shell", "open_url"}

# Итеративные инструменты: повтор с теми же аргументами валиден (экран уже
# изменился после прошлого вызова), защита от зацикливания их не трогает.
_DEDUP_EXEMPT = {"gui", "see_screen"}

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
# (кто сейчас работает: диспетчер-мозг, TARS-зрение, sandbox, хост, веб…).
_TOOL_ACTOR = {
    "gui": "ui-tars", "see_screen": "ui-tars",
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


# =========================================================================== #
# Системный промпт планировщика — «учебник» мозга.
#
# Собирается из именованных блоков: роль → алгоритм решения → протокол JSON →
# плейбуки (Windows-админ, Linux-админ, GUI, быт). Блоки — константы модуля,
# чтобы их можно было читать/править по отдельности, как главы методички.
# =========================================================================== #

_LANG_RULE = (
    "ЯЗЫК (ЖЁСТКОЕ ПРАВИЛО): думай и пиши ВСЕГДА и ТОЛЬКО на русском языке — и "
    "поле thought, и любые пояснения. НИКАКОГО украинского, английского или "
    "транслита латиницей (никаких «Teper nuzno», «Мені треба»). Имена команд, "
    "путей и кода — как есть, но рассуждения — строго по-русски."
)

_ROLE = (
    "Ты — JARVIS, единый мозг локальной агентной системы на этом компьютере. "
    "Твои две профессии:\n"
    "1) СИСТЕМНЫЙ АДМИНИСТРАТОР Windows и Linux с реальным доступом к хосту: "
    "команды ОС, PowerShell, службы, процессы, реестр, диски, сеть, файлы, "
    "установка программ, журналы, питание.\n"
    "2) ДОМАШНИЙ ПОМОЩНИК на все бытовые дела: погода, перевод, курсы, справки, "
    "счёт, поиск и чтение в интернете, музыка и громкость, напоминания, "
    "«открой/напиши/посчитай/найди».\n"
    "У тебя есть ГЛАЗА (скриншоты экрана) и РУКИ (команды, мышь, клавиатура) — "
    "ты видишь Windows GUI и управляешь им по-настоящему. Пользователь ждёт, "
    "что ты ДЕЛАЕШЬ, а не советуешь сделать. НИКОГДА не отвечай «я не могу» / "
    "«я всего лишь ИИ» — если есть подходящий инструмент, ВЫЗОВИ его и доведи "
    "задачу до конца."
)

_ALGORITHM = (
    "АЛГОРИТМ РАБОТЫ АДМИНИСТРАТОРА (следуй ему каждый ход):\n"
    "1. ПОНЯТЬ: чего хочет пользователь; на первом шаге в thought набросай "
    "короткий план из 2–4 пунктов и дальше следуй ему.\n"
    "2. ВЫБРАТЬ КАНАЛ исполнения — самый надёжный, а не самый эффектный:\n"
    "   • данные/настройки ОС, диагностика, установка → команда: windows "
    "(action=exec|powershell) — твой ГЛАВНЫЙ инструмент админа; сначала "
    "придумай команду, потом вызови;\n"
    "   • логика, расчёты, обработка данных/файлов → run_code или shell "
    "(изолированный sandbox);\n"
    "   • интерактивное окно БЕЗ CLI-пути (кнопки, мастера, меню, диалоги) → "
    "gui: сформулируй цель целиком, актуатор сам докликает;\n"
    "   • не знаешь, что сейчас на экране → СНАЧАЛА see_screen, потом решай;\n"
    "   • факты из внешнего мира → weather / web_search / web_fetch / wikipedia;\n"
    "   • о пользователе/прошлых договорённостях → memory_search.\n"
    "3. ДИАГНОСТИРОВАТЬ: команда вернула ошибку → ПРОЧИТАЙ текст ошибки и "
    "ПОМЕНЯЙ подход (исправь команду / другой канал / gui). Дословный повтор "
    "запрещён. «Access denied / требуются права администратора» → скажи об этом "
    "пользователю прямо и предложи путь (запустить мост от администратора) — "
    "не бейся об стену.\n"
    "4. ПРОВЕРИТЬ: после ИЗМЕНЕНИЯ состояния системы (служба, процесс, реестр, "
    "файл конфигурации, установка/удаление, файрвол) сделай ОДИН проверочный "
    "шаг ДРУГОЙ командой (статус/чтение) — и только потом отвечай. Настоящий "
    "админ не отчитывается «сделано», не убедившись. НО: для простых видимых "
    "действий (открыть приложение/сайт, вставить текст, показать данные) "
    "проверка НЕ нужна — успех инструмента означает «готово», сразу answer.\n"
    "5. ОТВЕТИТЬ: данных достаточно → action=answer. Не тяни лишние шаги."
)

_JSON_PROTOCOL = (
    "ПРОТОКОЛ ОТВЕТА (строго):\n"
    "• Отвечай ОДНИМ JSON-объектом, без текста вокруг.\n"
    "• Вызов инструмента: "
    '{"thought":"зачем","action":"<имя>","action_input":{...}}\n'
    "• Данных достаточно: {\"thought\":\"итог\",\"action\":\"answer\"}\n"
    "• Существуют ТОЛЬКО инструменты из списка выше. PowerShell — НЕ отдельный "
    "инструмент: это windows с action=\"powershell\". Пример: "
    '{"action":"windows","action_input":{"action":"powershell",'
    '"command":"Get-Process | Sort CPU -desc | Select -First 10 | Out-String"}}\n'
    "• Не выдумывай факты о погоде/вебе/файлах/экране — бери их инструментами.\n"
    "• Не повторяй успешный вызов с теми же аргументами: успех = шаг сделан.\n"
    "• Выполняй именно ПОСЛЕДНИЙ запрос пользователя; прерванную ранее задачу "
    "не возобновляй без явной просьбы.\n"
    "• Доводи до КОНЦА: «открой X и сделай Y» — это открыть И сделать Y.\n"
    "• Простой разговор/вопрос по общим знаниям — сразу answer."
)

_PLAYBOOK_WINDOWS = (
    "ПЛЕЙБУК: АДМИНИСТРИРОВАНИЕ WINDOWS (канал windows, action=powershell|exec; "
    "к PowerShell-командам с табличным выводом добавляй | Out-String):\n"
    "• Службы: Get-Service X; Start/Stop/Restart-Service X; проверка после — "
    "Get-Service X | Out-String.\n"
    "• Процессы: Get-Process | Sort CPU -desc | Select -First 10 | Out-String; "
    "завершить — Stop-Process -Name X -Force или action=kill_process.\n"
    "• Диски/место: Get-Volume | Out-String; Get-PSDrive C; здоровье — "
    "Get-PhysicalDisk | Out-String.\n"
    "• Память/CPU/GPU/сводка: инструмент system_info; GPU подробно — "
    "exec 'nvidia-smi'.\n"
    "• Сеть: Test-Connection 8.8.8.8 -Count 3; Get-NetIPConfiguration; "
    "Resolve-DnsName X; открытые порты — Get-NetTCPConnection -State Listen | "
    "Out-String; сброс DNS — exec 'ipconfig /flushdns'.\n"
    "• Реестр: Get-ItemProperty 'HKCU:\\...' ; менять — Set-ItemProperty, после "
    "чего Get-ItemProperty для проверки.\n"
    "• Установка/удаление программ: exec 'winget search X' → "
    "exec 'winget install --id <Id> -e --silent --accept-package-agreements "
    "--accept-source-agreements'; удалить — 'winget uninstall --id <Id>'.\n"
    "• Автозагрузка: Get-CimInstance Win32_StartupCommand | Out-String; "
    "планировщик: Get-ScheduledTask | Where State -eq 'Ready' | Out-String.\n"
    "• Журнал событий (диагностика сбоев): Get-WinEvent -LogName System "
    "-MaxEvents 20 | Out-String.\n"
    "• Файрвол: Get-NetFirewallProfile | Out-String; правило — New-NetFirewallRule.\n"
    "• Пользователи: Get-LocalUser | Out-String; net user X.\n"
    "• Файлы: action=read_file/write_file (пути с %USERPROFILE%, ~), листинг — "
    "инструмент list_dir; копировать/переносить — powershell Copy-Item/Move-Item.\n"
    "• Питание/блокировка: action=system_power (lock|shutdown|reboot|cancel).\n"
    "• КОНСОЛЬ — два разных случая: «покажи вывод» → exec/powershell с обычной "
    "командой БЕЗ -NoExit/-k (текст вернётся тебе, перескажешь); «открой окно "
    "консоли и оставь» → action=open_app command='powershell -NoExit -Command "
    "\"...\"' (или 'cmd /k \"...\"'). НИКОГДА не давай -NoExit/-k в exec — "
    "зависнет."
)

_PLAYBOOK_LINUX = (
    "ПЛЕЙБУК: АДМИНИСТРИРОВАНИЕ LINUX:\n"
    "• Канал тот же — windows.exec: на Linux-хосте команда идёт в bash "
    "НАПРЯМУЮ (exec command='df -h'); на Windows-хосте с WSL добавляй префикс: "
    "command='wsl bash -lc \"df -h\"'. Мост сам определяет ОС.\n"
    "• Службы: systemctl status X; systemctl start|stop|restart X; проверка "
    "после — systemctl is-active X; логи службы — journalctl -u X -n 50 "
    "--no-pager.\n"
    "• Пакеты: apt-get install -y X / apt-cache search X (или dnf/pacman — "
    "посмотри, что есть).\n"
    "• Диагностика: df -h; du -sh <путь>; free -h; top -bn1 | head -20; "
    "uname -a; uptime.\n"
    "• Сеть: ip a; ss -tlnp; ping -c3 X; dig X (или resolvectl query X).\n"
    "• Docker на хосте: docker ps; docker logs --tail 50 X; docker compose ps.\n"
    "• Права/владельцы: chmod/chown; пользователи: id, useradd, usermod.\n"
    "• Изолированные скрипты/эксперименты/обработка данных — инструмент shell "
    "(sandbox с bash/python3/node/gcc, каталог /workspace живёт всю сессию; "
    "реальный хост НЕ затрагивается)."
)

_PLAYBOOK_GUI = (
    "ПЛЕЙБУК: WINDOWS GUI — глаза и руки (это твоя ОБЯЗАТЕЛЬНАЯ способность, "
    "работает и на Linux-десктопе — мост транслирует):\n"
    "• РАЗЛИЧАЙ ввод текста и нажатие кнопок — это РАЗНОЕ:\n"
    "  – вставить ТЕКСТ в текстовое поле/редактор → windows action=paste_text "
    "(надёжно, через буфер);\n"
    "  – «нажми кнопку/выбери пункт/кликни/понажимай» → инструмент gui "
    "(реальные клики мышью);\n"
    "  – клавиши/сочетания в активном окне → windows action=send_keys "
    "(синтаксис SendKeys: '^s', '{ENTER}', '%{F4}').\n"
    "• gui — это суб-агент: ОДИН вызов с целью целиком («открой Параметры и "
    "включи тёмную тему»), внутри он сам смотрит скриншоты и докликивает до "
    "конца. Не дроби цель на отдельные клики.\n"
    "• see_screen — посмотреть и прочитать экран: «что открыто/что на экране/"
    "прочитай окно». Также используй его для ПРОВЕРКИ после важного gui-шага "
    "(увидеть, что диалог закрылся/настройка включилась). НЕ угадывай по списку "
    "процессов — посмотри.\n"
    "• Типовые связки:\n"
    "  – «напиши в Блокноте X»: open_app 'notepad' → paste_text X. НЕ сохраняй "
    "и НЕ закрывай, если не просили.\n"
    "  – «напиши код в VS Code»: write_file(path, content) → open_app "
    "'code \"<путь>\"' (текст уже внутри).\n"
    "  – «открой сайт/вкладку про X»: open_url (url или query) — открывай САМ.\n"
    "  – калькулятор: «понажимай кнопки» → gui; «просто посчитай» → "
    "калькулятор клавиатурой (send_keys '4*4=') или инструмент calculator.\n"
    "  – окно свернуть/развернуть/закрыть: send_keys ('%{F4}' — закрыть) или gui.\n"
    "• Если команда/скрипт не сработали, а цель достижима на экране — НЕ "
    "сдавайся: переходи на gui («не вышло командой — тыкаем визуально»)."
)

_PLAYBOOK_HOME = (
    "ПЛЕЙБУК: БЫТОВЫЕ ДЕЛА:\n"
    "• Погода — weather (город), НЕ web_search; перескажи словами.\n"
    "• Перевод — translate; курсы валют — exchange_rate; определения/справка — "
    "wikipedia / define; точный счёт — calculator; дата/время — now.\n"
    "• «Найди/загугли/что пишут про X» — web_search, при необходимости открой "
    "лучший результат через web_fetch и перескажи суть.\n"
    "• «Включи музыку/поставь на паузу/громче/тише/следующий трек» — windows "
    "action=media_hook (play_pause|next|prev|vol_up|vol_down|mute).\n"
    "• «Напомни мне в HH:MM про X» — Windows: exec 'schtasks /create /sc once "
    "/st HH:MM /tn jarvis_reminder_<N> /tr \"msg * Напоминание: X\" /f'; "
    "Linux: systemd-run --user --on-calendar=... ; после создания проверь "
    "(schtasks /query /tn ...).\n"
    "• «Запомни, что …» — memory_save; «помнишь/как я просил» — memory_search.\n"
    "• Анализ папки/проекта: list_dir(path) → windows.read_file по ключевым "
    "файлам → вывод по содержимому."
)


def _planner_system() -> str:
    return (
        f"{_LANG_RULE}\n\n"
        f"{_ROLE}\n\n"
        f"{_ALGORITHM}\n\n"
        "Доступные инструменты:\n"
        f"{_registry.specs()}\n"
        "(Инструменты с префиксом mcp_ — внешние подключаемые MCP-серверы; "
        "вызывай их по описанию, как обычные.)\n\n"
        f"{_JSON_PROTOCOL}\n\n"
        f"{_PLAYBOOK_WINDOWS}\n\n"
        f"{_PLAYBOOK_LINUX}\n\n"
        f"{_PLAYBOOK_GUI}\n\n"
        f"{_PLAYBOOK_HOME}\n\n"
        "И ГЛАВНОЕ — РАССУЖДАЙ, а не ищи задачу дословно в плейбуках: это "
        "ПРИМЕРЫ, а не предел. Нет готового рецепта ≠ задача невыполнима — "
        "почти всё делается через windows.exec/powershell, а что не делается "
        "командой — делается через gui."
    )


_ANSWER_SYSTEM = (
    "ЯЗЫК (ЖЁСТКОЕ ПРАВИЛО): отвечай ВСЕГДА и ТОЛЬКО на чистом русском языке. "
    "Никакого украинского, английского или транслита латиницей. Это не обсуждается.\n\n"
    "Ты — JARVIS: личный системный администратор Windows и Linux и помощник на "
    "все руки, с РЕАЛЬНЫМ доступом к компьютеру и интернету. Ты администрируешь "
    "обе ОС, видишь экран и управляешь окнами и мышью, пишешь и запускаешь код, "
    "рулишь службами/процессами/файлами, ищешь и читаешь веб, знаешь погоду и "
    "курсы. НИЖЕ — наблюдения от уже ВЫПОЛНЕННЫХ действий. Сформулируй финальный "
    "ответ по-русски, опираясь СТРОГО на эти наблюдения и историю диалога.\n"
    "ОТЧЁТ АДМИНИСТРАТОРА: если ты менял состояние системы — скажи, ЧТО сделано, "
    "каков РЕЗУЛЬТАТ и чем ПРОВЕРЕНО (если проверял). Если что-то не удалось — "
    "честно скажи, что именно, и предложи следующий шаг. Цифры и статусы бери из "
    "наблюдений, не приукрашивай.\n"
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
# Нормализация решения планировщика — терпимость к «почти правильным» вызовам
# --------------------------------------------------------------------------- #
# Действия инструмента windows: модель нередко называет их именем инструмента
# ("powershell", "open_app") или пишет через точку ("windows.exec"). Вместо
# ошибки «нет такого инструмента» такие вызовы молча чинятся.
_WINDOWS_ACTION_NAMES = {
    "exec", "powershell", "open_app", "screenshot", "media_hook", "kill_process",
    "system_power", "write_file", "read_file", "type_text", "key_press",
    "paste_text", "send_keys", "set_clipboard", "get_clipboard",
}
# Синонимы имён инструментов, которые модели галлюцинируют чаще всего.
_ACTION_SYNONYMS = {
    "ui-tars": "gui", "ui_tars": "gui", "uitars": "gui", "tars": "gui",
    "cmd": "exec", "bash": "exec", "terminal": "exec", "console": "exec",
    "search": "web_search", "google": "web_search",
    "browser": "open_url", "browse": "open_url",
    "screen": "see_screen", "look_screen": "see_screen",
    "final_answer": "answer", "finish": "answer", "final": "answer",
    "respond": "answer", "done": "answer", "reply": "answer",
}
# Во что завернуть строковый action_input для популярных инструментов.
_STRING_ARG_KEY = {
    "windows": "command", "shell": "command", "run_code": "code",
    "gui": "goal", "see_screen": "question", "web_search": "query",
    "web_fetch": "url", "open_url": "url", "translate": "text",
    "calculator": "expression", "weather": "location", "wikipedia": "query",
    "list_dir": "path", "memory_save": "text", "memory_search": "query",
}


def _normalize_plan(action: str, args: Any) -> tuple[str, dict[str, Any]]:
    """Починить «почти правильное» решение модели до валидного вызова."""
    action = str(action or "").strip().strip("\"'` ")

    # Форма через точку: windows.powershell / gui.click / tars.do → базовый инструмент
    if "." in action:
        head, _, tail = action.partition(".")
        head = head.strip().lower()
        if head == "windows" and tail.strip() in _WINDOWS_ACTION_NAMES:
            if isinstance(args, dict):
                args = {"action": tail.strip(), **args}
            action = "windows"
        elif head in ("gui", "tars", "uitars"):
            action = "gui"
        else:
            action = head

    low = action.lower()
    action = _ACTION_SYNONYMS.get(low, low) if low in _ACTION_SYNONYMS else action

    # Имя действия windows использовано как имя инструмента → завернуть
    if action in _WINDOWS_ACTION_NAMES and _registry.get(action) is None:
        if isinstance(args, dict):
            args = {"action": action, **args}
        else:
            key = "command" if action in ("exec", "powershell", "open_app") else "text"
            args = {"action": action, key: args}
        action = "windows"

    # Строковый/скалярный action_input → обернуть в правильный параметр
    if not isinstance(args, dict):
        args = {_STRING_ARG_KEY.get(action, "value"): args}
    return action, args


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
    seen_calls: set[str] = set()      # защита от зацикливания на одинаковых вызовах
    fail_streak: dict[str, int] = {}  # подряд идущие неудачи по инструментам

    # --- ФАЗА 1: планирование с инструментами ---
    try:
        for step in range(MAX_STEPS):
            plan = await _plan_next(session_id, scratch)
            raw_action = str(plan.get("action", "answer")).strip()
            thought = str(plan.get("thought", "")).strip()
            if thought:
                yield {"type": "thought", "text": thought, "actor": "dispatcher"}

            action, args = _normalize_plan(
                raw_action, plan.get("action_input") or plan.get("input") or {})
            if action in ("answer", ""):
                break

            tool = _registry.get(action)
            if tool is None:
                scratch.append({
                    "action": action, "args": {},
                    "observation": (f"Нет такого инструмента '{action}'. Выбери из "
                                    f"СПИСКА доступных; действия на хосте — это "
                                    f"инструмент windows с полем action.")})
                continue

            # СТРАХОВКА от зацикливания: тот же инструмент с теми же аргументами
            # уже вызывался — не повторяем (модель «залипла»), переходим к ответу.
            # Исключение — итеративные gui/see_screen: экран между вызовами
            # меняется, повтор цели валиден (gui доводит до конца собственный цикл).
            call_key = action + "::" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if action not in _DEDUP_EXEMPT and call_key in seen_calls:
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

            if result.get("ok"):
                fail_streak[action] = 0
            else:
                fail_streak[action] = fail_streak.get(action, 0) + 1
                # АВТО-FALLBACK CLI → GUI: консольное действие на ПК не сработало —
                # подсказываем мозгу довести задачу визуально.
                if action in _CLI_HOST_TOOLS:
                    observation += (
                        "\n[fallback] Командой это сделать не вышло. Если цель "
                        "достижима в графическом интерфейсе — вызови инструмент gui "
                        "(актуатор сам нажмёт нужное мышью); не понимаешь состояние "
                        "экрана — сначала see_screen. Не сдавайся на CLI.")
                # ЭСКАЛАЦИЯ: два провала подряд у одного инструмента — требуем
                # сменить подход, а не пробовать то же самое третий раз.
                if fail_streak[action] >= 2:
                    observation += (
                        f"\n[диагностика] У '{action}' уже {fail_streak[action]} "
                        "неудачи подряд. СМЕНИ ПОДХОД: перечитай текст ошибки, "
                        "возьми другую команду или другой канал (windows ↔ shell ↔ "
                        "gui), либо честно доложи пользователю, что мешает.")

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
