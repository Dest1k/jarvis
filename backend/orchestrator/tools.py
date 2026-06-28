# -*- coding: utf-8 -*-
"""
tools.py — реестр инструментов агента JARVIS-OS (агентская «прокладка» между
запросом пользователя и исполнителями).

Инструменты — это то, ЧЕМ оркестратор (Qwen) реально действует в мире:

    run_code      — написать/исполнить код (Python/Bash/C++/C/JS) в изолированном
                    sandbox-контейнере. Покрывает «напиши hello world на C++».
    windows       — команды на ХОСТЕ Windows через защищённый RPC-мост
                    (exec/powershell/open_app/screenshot/media/...). HITL-гейт
                    для деструктивного — на стороне моста.
    gui           — визуальное управление Windows через UI-TARS (скриншот → действие).
    web_fetch     — скачать страницу и извлечь текст (парсинг сайтов).
    web_search    — веб-поиск (DuckDuckGo, без ключей).
    weather       — погода через wttr.in (без ключей).
    memory_save   — записать факт в долговременную память.
    memory_search — найти факт в долговременной памяти.

Каждый инструмент возвращает dict {ok: bool, content: str}. content — это то,
что увидит модель как «наблюдение» (observation) на следующем шаге ReAct-цикла,
поэтому он КОРОТКИЙ и информативный (большие выводы обрезаются).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx

from . import llm

log = logging.getLogger("jarvis.tools")

SANDBOX_CONTAINER = os.environ.get("JARVIS_SANDBOX_CONTAINER", "jarvis-sandbox")
MAX_OBSERVATION_CHARS = 8000   # потолок наблюдения (защита контекстного окна)
HTTP_UA = "Mozilla/5.0 (JARVIS-OS agent) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[обрезано, всего {len(text)} символов]"


# --------------------------------------------------------------------------- #
# Контекст исполнения инструментов
# --------------------------------------------------------------------------- #
@dataclass
class ToolContext:
    """Зависимости, нужные инструментам во время исполнения."""
    bridge: Any = None                 # HostBridgeClient (RPC-мост)
    longterm: Any = None               # LongTermMemory
    session_id: str = "default"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, str]         # имя → описание (для промпта)
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

    def spec_line(self) -> str:
        params = ", ".join(f"{k} ({v})" for k, v in self.parameters.items()) or "—"
        return f"- {self.name}: {self.description}\n    параметры: {params}"


# =========================================================================== #
# Реализации инструментов
# =========================================================================== #

# --- 1. Исполнение кода в sandbox ------------------------------------------ #
_LANG_SPEC: dict[str, dict[str, str]] = {
    "python": {"file": "main.py", "run": "python3 main.py"},
    "bash":   {"file": "main.sh", "run": "bash main.sh"},
    "sh":     {"file": "main.sh", "run": "sh main.sh"},
    "cpp":    {"file": "main.cpp", "run": "g++ -O2 -std=c++17 main.cpp -o main && ./main"},
    "c":      {"file": "main.c",   "run": "gcc -O2 main.c -o main && ./main"},
    "javascript": {"file": "main.js", "run": "node main.js"},
    "node":   {"file": "main.js", "run": "node main.js"},
}


async def tool_run_code(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Скомпилировать/исполнить код в изолированном sandbox-контейнере."""
    language = str(args.get("language", "python")).lower().strip()
    code = args.get("code", "")
    stdin = args.get("stdin", "")
    timeout = int(args.get("timeout", 60))

    spec = _LANG_SPEC.get(language)
    if spec is None:
        return {"ok": False,
                "content": f"Неподдерживаемый язык '{language}'. "
                           f"Доступно: {', '.join(sorted(_LANG_SPEC))}."}
    if not code.strip():
        return {"ok": False, "content": "Пустой код."}

    run_id = f"run_{int(time.time())}_{os.getpid() % 100000}"
    workdir = f"/workspace/{run_id}"
    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    stdin_b64 = base64.b64encode(stdin.encode("utf-8")).decode("ascii")

    # Сборку и запуск делаем внутри одного bash-вызова. Источник и stdin
    # передаём base64, чтобы не страдать от кавычек/переводов строк.
    inner = (
        f"set -e; mkdir -p {workdir}; cd {workdir}; "
        f"echo {code_b64} | base64 -d > {spec['file']}; "
        f"echo {stdin_b64} | base64 -d > .stdin; "
        f"timeout {timeout} bash -lc '{spec['run']}' < .stdin; "
    )
    from . import dockerapi
    try:
        rc, output = await dockerapi.exec_run(
            SANDBOX_CONTAINER, ["bash", "-lc", inner], timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False,
                "content": f"Не удалось выполнить код в sandbox (Docker API): {exc}"}

    # уборка рабочего каталога (не критично при ошибке)
    asyncio.create_task(_sandbox_cleanup(workdir))

    body = f"[код возврата: {rc}]\n"
    if output:
        body += output if output.endswith("\n") else output + "\n"
    return {"ok": rc == 0, "content": _truncate(body),
            "data": {"returncode": rc, "output": output,
                     "language": language, "code": code}}


async def _sandbox_cleanup(workdir: str) -> None:
    try:
        from . import dockerapi
        await dockerapi.exec_run(SANDBOX_CONTAINER, ["rm", "-rf", workdir], timeout=15)
    except Exception:  # noqa: BLE001
        pass


# --- 2. Команды на хосте Windows (RPC-мост) -------------------------------- #
_WINDOWS_ACTIONS = {
    "exec", "powershell", "open_app", "screenshot",
    "media_hook", "kill_process", "system_power",
}


async def tool_windows(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Выполнить действие на ХОСТЕ Windows через защищённый RPC-мост."""
    if ctx.bridge is None:
        return {"ok": False, "content": "RPC-мост недоступен (нет объекта моста)."}
    action = str(args.get("action", "")).strip()
    if action not in _WINDOWS_ACTIONS:
        return {"ok": False,
                "content": f"Неизвестное действие '{action}'. "
                           f"Доступно: {', '.join(sorted(_WINDOWS_ACTIONS))}."}

    payload: dict[str, Any] = {}
    if action in ("exec", "powershell", "open_app"):
        payload["command"] = args.get("command", "")
        if not payload["command"]:
            return {"ok": False, "content": "Не задан параметр 'command'."}
    elif action == "media_hook":
        payload["key"] = args.get("key", "play_pause")
    elif action == "kill_process":
        payload["name"] = args.get("name", "")
    elif action == "system_power":
        payload["mode"] = args.get("mode", "")

    res = await ctx.bridge.call(action, payload)
    if not res.get("ok"):
        err = res.get("error", "ошибка")
        if res.get("halted"):
            return {"ok": False, "content": f"Операция остановлена HITL-гейтом: {err}"}
        return {"ok": False, "content": f"Хост вернул ошибку: {err}"}
    result = res.get("result", {}) or {}
    out = ((result.get("stdout") or "") + (result.get("stderr") or "")).strip()
    if out:
        return {"ok": True, "content": _truncate(out)}
    # Пустой вывод при успехе — норма для open_app и команд без stdout. Делаем
    # ответ ОДНОЗНАЧНЫМ «выполнено», иначе модель не понимает, что цель достигнута,
    # и повторяет тот же вызов снова и снова.
    done = {
        "open_app": "Приложение/ссылка успешно запущены на хосте.",
        "exec": "Команда выполнена успешно (вывод пуст).",
        "powershell": "PowerShell-команда выполнена успешно (вывод пуст).",
        "media_hook": "Медиа-команда отправлена.",
        "screenshot": "Скриншот сделан.",
    }.get(action, f"Действие '{action}' выполнено успешно.")
    return {"ok": True, "content": done}


# --- 3. Визуальное управление GUI через UI-TARS --------------------------- #
async def tool_gui(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """
    Один шаг визуального управления Windows: снять скриншот хоста, передать его
    UI-TARS вместе с целью, получить ОДНО действие и выполнить его через мост.

    Это лучшее-усилие: точное GUI-управление сильно зависит от модели/железа,
    поэтому действия атомарные, а оркестратор вызывает gui повторно по шагам.
    """
    if ctx.bridge is None:
        return {"ok": False, "content": "RPC-мост недоступен — GUI-управление невозможно."}
    goal = args.get("goal", "")
    if not goal:
        return {"ok": False, "content": "Не задана цель 'goal'."}

    shot = await ctx.bridge.call("screenshot", {"return_b64": True})
    result = (shot.get("result", {}) or {})
    img_b64 = result.get("image_b64")
    if not shot.get("ok") or not img_b64:
        return {"ok": False,
                "content": "Не удалось получить скриншот хоста "
                           f"({shot.get('error', 'нет image_b64')})."}
    screen_w = int(result.get("screen_w", 1920))
    screen_h = int(result.get("screen_h", 1080))

    system = (
        "Ты — UI-TARS, визуальный контроллер Windows. По скриншоту и цели верни "
        "СТРОГО один JSON-объект следующего шага и ничего больше:\n"
        '{"action":"click|double_click|right_click|type|key|scroll|done|fail",'
        '"x":<0-1000>,"y":<0-1000>,"text":"...","key":"...","amount":<int>,'
        '"reason":"кратко"}\n'
        "Координаты x,y — НОРМИРОВАННЫЕ 0..1000 относительно ширины/высоты экрана. "
        "Для 'type' заполни text; для 'key' — key (например 'enter','ctrl+s'); "
        "для 'scroll' — amount (отрицательное = вниз). action='done' когда цель "
        "достигнута, 'fail' — если невозможно."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": f"Цель: {goal}"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
        ]},
    ]
    try:
        raw = await llm.chat(messages, base_url=llm.UITARS_URL, model=llm.UITARS_MODEL,
                             temperature=0.0, max_tokens=256, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"UI-TARS недоступен: {exc}"}

    act = llm.extract_json(raw) or _parse_uitars_native(raw)
    if not act:
        return {"ok": False, "content": f"UI-TARS вернул неразборчивый ответ: {raw[:300]}"}

    action = str(act.get("action", "")).lower()
    if action in ("done", "fail"):
        return {"ok": action == "done",
                "content": f"UI-TARS: {action} — {act.get('reason', '')}"}

    # нормированные → пиксели
    px = int(float(act.get("x", 0)) / 1000.0 * screen_w)
    py = int(float(act.get("y", 0)) / 1000.0 * screen_h)

    if action in ("click", "double_click", "right_click"):
        button = "right" if action == "right_click" else "left"
        res = await ctx.bridge.call("mouse_click", {
            "x": px, "y": py, "button": button,
            "double": action == "double_click"})
    elif action == "type":
        res = await ctx.bridge.call("type_text", {"text": act.get("text", "")})
    elif action == "key":
        res = await ctx.bridge.call("key_press", {"keys": act.get("key", "")})
    elif action == "scroll":
        res = await ctx.bridge.call("scroll", {"amount": int(act.get("amount", -3))})
    else:
        return {"ok": False, "content": f"Неизвестное GUI-действие: {action}"}

    ok = bool(res.get("ok"))
    return {"ok": ok,
            "content": f"Выполнено GUI-действие '{action}' "
                       f"({act.get('reason', '')}). "
                       f"{'' if ok else 'Мост: ' + str(res.get('error'))}"}


def _parse_uitars_native(text: str) -> Optional[dict[str, Any]]:
    """Запасной разбор «родного» формата UI-TARS: click(start_box='(x,y)') и т.п."""
    m = re.search(r"(\w+)\s*\(\s*start_box\s*=\s*'?\(?(\d+)\s*,\s*(\d+)", text)
    if m:
        kind = m.group(1).lower()
        x, y = int(m.group(2)), int(m.group(3))
        # UI-TARS обычно нормирует в 0..1000 — оставляем как есть
        return {"action": "click" if "click" in kind else kind, "x": x, "y": y}
    if "finished" in text.lower() or "done" in text.lower():
        return {"action": "done", "reason": text[:120]}
    return None


# --- 4. Веб: скачать и извлечь текст --------------------------------------- #
async def tool_web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Скачать URL и вернуть извлечённый текст (для парсинга сайтов)."""
    url = str(args.get("url", "")).strip()
    if not url:
        return {"ok": False, "content": "Не задан 'url'."}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": HTTP_UA}) as cli:
            r = await cli.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "html" in ctype or r.text.lstrip().startswith("<"):
                text = _html_to_text(r.text)
            else:
                text = r.text
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "content": f"HTTP {exc.response.status_code} для {url}."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Ошибка загрузки {url}: {exc}"}
    return {"ok": True, "content": _truncate(f"Источник: {url}\n\n{text}")}


def _html_to_text(html: str) -> str:
    """HTML → читаемый текст. Пробуем BeautifulSoup, иначе — грубая чистка regex."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "header", "footer"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:  # noqa: BLE001
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# --- 5. Веб-поиск (DuckDuckGo, без ключей) --------------------------------- #
async def tool_web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Поиск в вебе через DuckDuckGo HTML (без API-ключа)."""
    query = str(args.get("query", "")).strip()
    if not query:
        return {"ok": False, "content": "Не задан 'query'."}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": HTTP_UA}) as cli:
            r = await cli.post("https://html.duckduckgo.com/html/",
                               data={"q": query})
            r.raise_for_status()
            results = _parse_ddg(r.text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Ошибка поиска: {exc}"}
    if not results:
        return {"ok": True, "content": f"По запросу «{query}» ничего не найдено."}
    lines = [f"{i+1}. {it['title']}\n   {it['url']}\n   {it['snippet']}"
             for i, it in enumerate(results[:6])]
    return {"ok": True, "content": _truncate("Результаты поиска:\n" + "\n".join(lines))}


def _parse_ddg(html: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for res in soup.select(".result"):
            a = res.select_one(".result__a")
            sn = res.select_one(".result__snippet")
            if not a:
                continue
            out.append({
                "title": a.get_text(" ", strip=True),
                "url": a.get("href", ""),
                "snippet": sn.get_text(" ", strip=True) if sn else "",
            })
    except Exception:  # noqa: BLE001
        # грубый фолбэк
        for m in re.finditer(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html):
            out.append({"title": re.sub("<[^>]+>", "", m.group(2)),
                        "url": m.group(1), "snippet": ""})
    return out


# --- 6. Погода (wttr.in, без ключей) --------------------------------------- #
async def tool_weather(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Прогноз погоды через wttr.in (без ключей). Покрывает «погода на завтра»."""
    location = str(args.get("location", "")).strip()
    loc_path = location.replace(" ", "+") if location else ""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "curl/8"}) as cli:
            r = await cli.get(f"https://wttr.in/{loc_path}?format=j1&lang=ru")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Не удалось получить погоду: {exc}"}

    try:
        area = data.get("nearest_area", [{}])[0]
        city = (area.get("areaName", [{}])[0].get("value")
                or location or "запрошенная локация")
        cur = data.get("current_condition", [{}])[0]
        now = (f"Сейчас в {city}: {cur.get('temp_C')}°C "
               f"(ощущается {cur.get('FeelsLikeC')}°C), "
               f"{_ru_desc(cur)}, ветер {cur.get('windspeedKmph')} км/ч, "
               f"влажность {cur.get('humidity')}%.")
        days = data.get("weather", [])
        forecast_lines = []
        for d in days[:3]:
            date = d.get("date")
            mn, mx = d.get("mintempC"), d.get("maxtempC")
            midday = d.get("hourly", [{}])[len(d.get("hourly", [])) // 2] if d.get("hourly") else {}
            forecast_lines.append(f"{date}: от {mn}°C до {mx}°C, {_ru_desc(midday)}.")
        body = now + "\nПрогноз:\n" + "\n".join(forecast_lines)
    except Exception:  # noqa: BLE001
        body = json.dumps(data, ensure_ascii=False)[:1500]
    return {"ok": True, "content": _truncate(body)}


def _ru_desc(cond: dict[str, Any]) -> str:
    arr = cond.get("lang_ru") or cond.get("weatherDesc") or []
    if arr and isinstance(arr, list):
        return arr[0].get("value", "")
    return ""


# --- 7. Память ------------------------------------------------------------- #
async def tool_memory_save(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Сохранить факт/заметку в долговременную память."""
    text = str(args.get("text", "")).strip()
    if not text:
        return {"ok": False, "content": "Пустая заметка."}
    if ctx.longterm is None:
        return {"ok": False, "content": "Долговременная память недоступна."}
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    item = ctx.longterm.save(text, tags=tags, kind="fact")
    return {"ok": True, "content": f"Сохранено в память (id={item['id']})."}


async def tool_memory_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Найти факты в долговременной памяти."""
    if ctx.longterm is None:
        return {"ok": False, "content": "Долговременная память недоступна."}
    query = str(args.get("query", "")).strip()
    hits = ctx.longterm.search(query, k=5)
    if not hits:
        return {"ok": True, "content": "В памяти ничего не найдено."}
    lines = [f"- ({h['kind']}) {h['text']}" for h in hits]
    return {"ok": True, "content": _truncate("Найдено в памяти:\n" + "\n".join(lines))}


# =========================================================================== #
# Реестр
# =========================================================================== #
class ToolRegistry:
    """Каталог инструментов + диспетчер их вызова."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.add(Tool(
            "run_code",
            "Исполнить код в изолированном sandbox и вернуть вывод. "
            "Подходит для вычислений, генерации/проверки программ, демонстрации "
            "результата (напр. «hello world на C++»).",
            {"language": "python|bash|cpp|c|javascript",
             "code": "полный исходный код",
             "stdin": "(опц.) ввод на stdin",
             "timeout": "(опц.) лимит секунд, по умолч. 60"},
            tool_run_code,
        ))
        self.add(Tool(
            "windows",
            "Выполнить действие на ХОСТ-машине Windows через защищённый мост: "
            "запуск приложений, команды cmd/PowerShell, скриншот, медиа-клавиши, "
            "управление процессами/питанием. Деструктивное проходит подтверждение.",
            {"action": "exec|powershell|open_app|screenshot|media_hook|kill_process|system_power",
             "command": "для exec/powershell/open_app",
             "key": "для media_hook (play_pause|next|prev|vol_up|vol_down|mute)",
             "name": "имя процесса для kill_process",
             "mode": "для system_power (lock|shutdown|reboot|cancel)"},
            tool_windows,
        ))
        self.add(Tool(
            "gui",
            "Визуально управлять Windows через UI-TARS: один шаг (скриншот → "
            "клик/ввод/прокрутка). Использовать, когда нет CLI-способа, только "
            "графический. Вызывать повторно по шагам до достижения цели.",
            {"goal": "что нужно сделать на экране (одним коротким шагом или целью)"},
            tool_gui,
        ))
        self.add(Tool(
            "web_fetch",
            "Скачать веб-страницу и вернуть её текст (парсинг/чтение сайта).",
            {"url": "адрес страницы"},
            tool_web_fetch,
        ))
        self.add(Tool(
            "web_search",
            "Поиск в интернете. Возвращает заголовки, ссылки и сниппеты.",
            {"query": "поисковый запрос"},
            tool_web_search,
        ))
        self.add(Tool(
            "weather",
            "Текущая погода и прогноз на ближайшие дни для города/локации.",
            {"location": "город или место (пусто = по IP)"},
            tool_weather,
        ))
        self.add(Tool(
            "memory_save",
            "Запомнить факт о пользователе/системе в долговременную память.",
            {"text": "что запомнить", "tags": "(опц.) метки через запятую"},
            tool_memory_save,
        ))
        self.add(Tool(
            "memory_search",
            "Вспомнить факты из долговременной памяти по запросу.",
            {"query": "что вспомнить"},
            tool_memory_search,
        ))

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> str:
        """Текстовое описание всех инструментов для системного промпта."""
        return "\n".join(t.spec_line() for t in self._tools.values())

    async def run(self, name: str, args: dict[str, Any],
                  ctx: ToolContext) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False,
                    "content": f"Инструмент '{name}' не найден. "
                               f"Доступны: {', '.join(self.names())}."}
        log.info("Вызов инструмента %s args=%s",
                 name, json.dumps(args, ensure_ascii=False)[:200])
        try:
            return await tool.handler(args or {}, ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("Инструмент %s упал", name)
            return {"ok": False, "content": f"Инструмент '{name}' завершился ошибкой: {exc}"}
