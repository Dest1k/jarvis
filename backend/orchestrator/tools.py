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

import ast
import asyncio
import base64
import datetime as _datetime
import json
import logging
import math
import operator as _operator
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

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
    "write_file", "read_file", "type_text", "key_press",
    "paste_text", "send_keys", "set_clipboard", "get_clipboard",
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
    elif action == "write_file":
        payload["path"] = args.get("path", "")
        payload["content"] = args.get("content", "")
        if not payload["path"]:
            return {"ok": False, "content": "Не задан 'path' для write_file."}
    elif action == "read_file":
        payload["path"] = args.get("path", "")
        if not payload["path"]:
            return {"ok": False, "content": "Не задан 'path' для read_file."}
    elif action == "type_text":
        payload["text"] = args.get("text", "")
    elif action in ("paste_text", "set_clipboard"):
        payload["text"] = args.get("text", "")
    elif action == "key_press":
        payload["keys"] = args.get("keys", "")
    elif action == "send_keys":
        payload["keys"] = args.get("keys", "")

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
        "paste_text": "Текст вставлен в активное окно (через буфер обмена + Ctrl+V).",
        "set_clipboard": "Текст помещён в буфер обмена.",
        "send_keys": "Клавиши отправлены в активное окно.",
        "type_text": "Текст напечатан в активном окне.",
        "get_clipboard": "Буфер обмена пуст.",
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


# --- 6. Погода (wttr.in + фолбэк open-meteo, без ключей) ------------------- #
async def tool_weather(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """
    Текущая погода и прогноз на ближайшие дни. Два независимых источника без
    ключей: сначала wttr.in, при сбое — open-meteo (через геокодинг). Так
    инструмент не «отваливается» из-за недоступности одного сервиса.
    """
    location = str(args.get("location", "")).strip()
    body = await _weather_wttr(location)
    if not body:
        body = await _weather_openmeteo(location)
    if not body:
        return {"ok": False,
                "content": "Не удалось получить погоду ни из одного источника "
                           "(wttr.in и open-meteo недоступны)."}
    return {"ok": True, "content": _truncate(body)}


async def _weather_wttr(location: str) -> Optional[str]:
    loc_path = location.replace(" ", "+") if location else ""
    try:
        async with httpx.AsyncClient(timeout=18, follow_redirects=True,
                                     headers={"User-Agent": "curl/8"}) as cli:
            r = await cli.get(f"https://wttr.in/{loc_path}?format=j1&lang=ru")
            r.raise_for_status()
            data = r.json()
        area = data.get("nearest_area", [{}])[0]
        city = (area.get("areaName", [{}])[0].get("value") or location or "локация")
        cur = data.get("current_condition", [{}])[0]
        now = (f"Сейчас в {city}: {cur.get('temp_C')}°C "
               f"(ощущается {cur.get('FeelsLikeC')}°C), {_ru_desc(cur)}, "
               f"ветер {cur.get('windspeedKmph')} км/ч, влажность {cur.get('humidity')}%.")
        lines = []
        for d in data.get("weather", [])[:3]:
            hourly = d.get("hourly", [])
            midday = hourly[len(hourly) // 2] if hourly else {}
            lines.append(f"{d.get('date')}: от {d.get('mintempC')}°C до "
                         f"{d.get('maxtempC')}°C, {_ru_desc(midday)}.")
        return now + "\nПрогноз:\n" + "\n".join(lines)
    except Exception:  # noqa: BLE001
        return None


# Коды погоды WMO (open-meteo) → русское описание.
_WMO_RU = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "слабая морось", 53: "морось", 55: "сильная морось",
    61: "слабый дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь",
    67: "сильный ледяной дождь", 71: "слабый снег", 73: "снег", 75: "сильный снег",
    77: "снежная крупа", 80: "слабый ливень", 81: "ливень", 82: "сильный ливень",
    85: "снегопад", 86: "сильный снегопад", 95: "гроза", 96: "гроза с градом",
    99: "сильная гроза с градом",
}


async def _weather_openmeteo(location: str) -> Optional[str]:
    if not location:
        return None  # без города open-meteo не определит точку (нет геолокации по IP)
    try:
        async with httpx.AsyncClient(timeout=18, follow_redirects=True) as cli:
            g = await cli.get("https://geocoding-api.open-meteo.com/v1/search",
                              params={"name": location, "count": 1, "language": "ru"})
            results = (g.json() or {}).get("results") or []
            if not results:
                return None
            geo = results[0]
            lat, lon, name = geo["latitude"], geo["longitude"], geo.get("name", location)
            f = await cli.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "current_weather": True,
                "daily": "temperature_2m_max,temperature_2m_min,weathercode",
                "timezone": "auto", "forecast_days": 3})
            d = f.json()
        cur = d.get("current_weather", {})
        lines = [f"Сейчас в {name}: {cur.get('temperature')}°C, "
                 f"{_WMO_RU.get(cur.get('weathercode'), '')}, "
                 f"ветер {cur.get('windspeed')} км/ч."]
        daily = d.get("daily", {})
        dates = daily.get("time", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        codes = daily.get("weathercode", [])
        lines.append("Прогноз:")
        for i, dt in enumerate(dates[:3]):
            lines.append(f"{dt}: от {tmin[i]}°C до {tmax[i]}°C, "
                         f"{_WMO_RU.get(codes[i], '')}.")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return None


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


async def tool_list_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Перечислить всё, что сохранено в долговременной памяти."""
    if ctx.longterm is None:
        return {"ok": False, "content": "Долговременная память недоступна."}
    items = ctx.longterm.all()[:30]
    if not items:
        return {"ok": True, "content": "Долговременная память пуста."}
    lines = [f"- ({h['kind']}) {h['text']}" for h in items]
    return {"ok": True, "content": _truncate("Долговременная память:\n" + "\n".join(lines))}


# --- 8. Дата и время -------------------------------------------------------- #
async def tool_now(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Текущие дата и время (у модели нет встроенных часов)."""
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота",
            "воскресенье"]
    now = _datetime.datetime.now()
    utc = _datetime.datetime.now(_datetime.timezone.utc)
    return {"ok": True, "content":
            f"Локальное время сервера: {now:%Y-%m-%d %H:%M:%S}, {days[now.weekday()]}.\n"
            f"UTC: {utc:%Y-%m-%d %H:%M:%S}."}


# --- 9. Калькулятор (безопасный разбор выражения) --------------------------- #
_CALC_BINOPS = {
    ast.Add: _operator.add, ast.Sub: _operator.sub, ast.Mult: _operator.mul,
    ast.Div: _operator.truediv, ast.FloorDiv: _operator.floordiv,
    ast.Mod: _operator.mod, ast.Pow: _operator.pow,
}
_CALC_UNARY = {ast.UAdd: _operator.pos, ast.USub: _operator.neg}
_CALC_FUNCS: dict[str, Any] = {
    name: getattr(math, name) for name in (
        "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "log",
        "log2", "log10", "exp", "floor", "ceil", "factorial", "degrees",
        "radians", "hypot", "fabs", "gcd") if hasattr(math, name)
}
def _safe_factorial(n: Any) -> int:
    if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > 1000:
        raise ValueError("факториал определён для целых 0..1000")
    return math.factorial(n)


_CALC_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})
if "factorial" in _CALC_FUNCS:
    _CALC_FUNCS["factorial"] = _safe_factorial  # защита от блокировки event loop
_CALC_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _calc_eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _calc_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("допустимы только числа")
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BINOPS:
        left = _calc_eval(node.left)
        right = _calc_eval(node.right)
        # Защита от взрыва вычислений (блокирует event loop, ест память):
        # ограничиваем показатель степени — '9**9**9' и т.п. отвергаются.
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > 1000:
            raise ValueError("слишком большая степень (макс. 1000)")
        return _CALC_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARY:
        return _CALC_UNARY[type(node.op)](_calc_eval(node.operand))
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _CALC_FUNCS):
        return _CALC_FUNCS[node.func.id](*[_calc_eval(a) for a in node.args])
    if isinstance(node, ast.Name) and node.id in _CALC_NAMES:
        return _CALC_NAMES[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_calc_eval(e) for e in node.elts]
    raise ValueError("недопустимое выражение")


async def tool_calculator(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Безопасно вычислить математическое выражение (без запуска sandbox)."""
    expr = str(args.get("expression") or args.get("expr") or "").strip()
    if not expr:
        return {"ok": False, "content": "Не задано выражение."}
    try:
        value = _calc_eval(ast.parse(expr, mode="eval"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Не удалось вычислить '{expr}': {exc}"}
    return {"ok": True, "content": f"{expr} = {value}"}


# --- 10. Универсальный HTTP-запрос ----------------------------------------- #
async def tool_http_request(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Произвольный HTTP-запрос к любому API (GET/POST/...). Возвращает статус и тело."""
    method = str(args.get("method", "GET")).upper()
    url = str(args.get("url", "")).strip()
    if not url:
        return {"ok": False, "content": "Не задан 'url'."}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    headers = {"User-Agent": HTTP_UA, **(args.get("headers") or {})}
    params = args.get("params") or None
    js = args.get("json")
    body = args.get("body")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as cli:
            r = await cli.request(
                method, url, headers=headers, params=params, json=js,
                content=body if isinstance(body, (str, bytes)) else None)
        text = r.text
        if "json" in r.headers.get("content-type", ""):
            try:
                text = json.dumps(r.json(), ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": r.is_success,
                "content": _truncate(f"HTTP {r.status_code} {method} {url}\n\n{text}")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Ошибка HTTP-запроса: {exc}"}


# --- 11. Wikipedia ---------------------------------------------------------- #
async def tool_wikipedia(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Краткая выжимка статьи Wikipedia по теме."""
    title = str(args.get("query") or args.get("title") or "").strip()
    if not title:
        return {"ok": False, "content": "Не задан запрос."}
    lang = (str(args.get("lang", "ru")).strip() or "ru")
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": HTTP_UA}) as cli:
            r = await cli.get(url)
            if r.status_code == 404:
                return {"ok": False,
                        "content": f"Статья '{title}' не найдена в Wikipedia ({lang})."}
            r.raise_for_status()
            d = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Ошибка Wikipedia: {exc}"}
    page = d.get("content_urls", {}).get("desktop", {}).get("page", "")
    return {"ok": True, "content":
            _truncate(f"{d.get('title', title)}\n{d.get('extract', '')}\n{page}")}


# --- 12. Курсы валют (без ключей) ------------------------------------------ #
async def tool_exchange_rate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Курсы валют / конвертация (open.er-api.com, без ключей)."""
    base = str(args.get("base", "USD")).upper().strip() or "USD"
    target = args.get("target") or args.get("symbols")
    try:
        amount = float(args.get("amount", 1) or 1)
    except (TypeError, ValueError):
        amount = 1.0
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cli:
            r = await cli.get(f"https://open.er-api.com/v6/latest/{base}")
            r.raise_for_status()
            d = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Ошибка курсов: {exc}"}
    if d.get("result") != "success":
        return {"ok": False, "content": f"Не удалось получить курсы для {base}."}
    rates = d.get("rates", {})
    if target:
        tlist = ([t.strip().upper() for t in target] if isinstance(target, list)
                 else [t.strip().upper() for t in str(target).replace(",", " ").split()])
    else:
        tlist = [t for t in ("USD", "EUR", "RUB", "CNY", "GBP", "JPY", "UAH") if t != base]
    lines = [f"{amount:g} {base} = {round(amount * rates[t], 4):g} {t}"
             for t in tlist if t in rates]
    body = "\n".join(lines) or "Указанные валюты не найдены."
    return {"ok": True, "content":
            _truncate(f"Курсы {base} ({d.get('time_last_update_utc', '')}):\n{body}")}


# --- 13. Словарь (англ., без ключей) --------------------------------------- #
async def tool_define(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Определение английского слова (dictionaryapi.dev, без ключей)."""
    word = str(args.get("word") or args.get("query") or "").strip()
    if not word:
        return {"ok": False, "content": "Не задано слово."}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": HTTP_UA}) as cli:
            r = await cli.get(
                f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word)}")
            if r.status_code == 404:
                return {"ok": True, "content": f"Определение '{word}' не найдено."}
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "content": f"Ошибка словаря: {exc}"}
    out: list[str] = []
    for entry in data[:1]:
        for m in entry.get("meanings", [])[:3]:
            defs = [d.get("definition", "") for d in m.get("definitions", [])[:2]]
            out.append(f"[{m.get('partOfSpeech', '')}] " + "; ".join(defs))
    return {"ok": True, "content": _truncate(f"{word}:\n" + "\n".join(out))}


# --- 14. Информация о системе хоста ---------------------------------------- #
async def tool_system_info(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Сводка о ХОСТ-машине: ОС, CPU, RAM, аптайм и GPU (VRAM/загрузка)."""
    if ctx.bridge is None:
        return {"ok": False, "content": "RPC-мост недоступен."}
    ps = (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$cpu=(Get-CimInstance Win32_Processor).Name;"
        "$free=[math]::Round($os.FreePhysicalMemory/1MB,1);"
        "$tot=[math]::Round($os.TotalVisibleMemorySize/1MB,1);"
        "Write-Output \"ОС: $($os.Caption)\";"
        "Write-Output \"CPU: $cpu\";"
        "Write-Output \"RAM: занято $([math]::Round($tot-$free,1)) из $tot ГБ\""
    )
    res = await ctx.bridge.call("powershell", {"command": ps})
    info = ((res.get("result", {}) or {}).get("stdout", "")).strip()
    gpu = await ctx.bridge.call("exec", {"command":
        "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu "
        "--format=csv,noheader,nounits"})
    gpu_out = ((gpu.get("result", {}) or {}).get("stdout", "")).strip()
    body = info + (f"\nGPU: {gpu_out}" if gpu_out else "")
    return {"ok": bool(info), "content": _truncate(body or "Нет данных о системе.")}


# --- 15. Открыть веб-страницу/поиск в браузере хоста ----------------------- #
async def tool_open_url(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Открыть URL (или поисковый запрос) в браузере по умолчанию на ПК."""
    if ctx.bridge is None:
        return {"ok": False, "content": "RPC-мост недоступен."}
    url = str(args.get("url", "")).strip()
    query = str(args.get("query", "")).strip()
    if not url and query:
        url = "https://www.google.com/search?q=" + quote(query)
    if not url:
        return {"ok": False, "content": "Нужен 'url' или 'query'."}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    res = await ctx.bridge.call("open_app", {"command": url})
    if res.get("ok"):
        return {"ok": True, "content": f"Открыл в браузере: {url}"}
    return {"ok": False, "content": f"Не удалось открыть {url}: {res.get('error', '')}"}


# --- 16. Листинг каталога на хосте ----------------------------------------- #
async def tool_list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Перечислить файлы в папке на ПК (для анализа проектов/каталогов)."""
    if ctx.bridge is None:
        return {"ok": False, "content": "RPC-мост недоступен."}
    path = str(args.get("path", "")).strip()
    if not path:
        return {"ok": False, "content": "Не задан 'path'."}
    res = await ctx.bridge.call("list_dir", {"path": path})
    result = res.get("result", {}) or {}
    out = (result.get("stdout") or result.get("stderr") or "").strip()
    return {"ok": res.get("ok", False), "content": _truncate(out or "(пусто)")}


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
            "Полноценное взаимодействие с ХОСТ-машиной Windows через защищённый мост: "
            "запуск программ, ВВОД ТЕКСТА в активное окно (paste_text — надёжно, через "
            "буфер обмена), управляющие клавиши (send_keys), создание/чтение файлов, "
            "команды cmd/PowerShell, скриншот, медиа, процессы/питание. "
            "Чтобы 'написать текст/код в программе' — open_app, затем paste_text. "
            "Деструктивное — с подтверждением оператора.",
            {"action": "open_app|paste_text|send_keys|write_file|read_file|exec|powershell|"
                       "set_clipboard|type_text|key_press|screenshot|media_hook|"
                       "kill_process|system_power",
             "command": "для open_app (имя программы/файл/URL) и exec/powershell",
             "text": "текст для paste_text/set_clipboard/type_text",
             "keys": "для send_keys (синтаксис SendKeys: '^s','{ENTER}','^a') или key_press",
             "path": "для write_file/read_file (поддержка %USERPROFILE%, ~)",
             "content": "содержимое для write_file",
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
        self.add(Tool(
            "list_memory",
            "Показать всё содержимое долговременной памяти.",
            {},
            tool_list_memory,
        ))
        self.add(Tool(
            "now",
            "Текущие дата, день недели и время (у модели нет встроенных часов).",
            {},
            tool_now,
        ))
        self.add(Tool(
            "calculator",
            "Точно вычислить математическое выражение (арифметика, функции math, "
            "константы pi/e). Быстрее и дешевле, чем run_code.",
            {"expression": "напр. '2**10 + sqrt(144)' или 'sin(pi/2)'"},
            tool_calculator,
        ))
        self.add(Tool(
            "http_request",
            "Произвольный HTTP-запрос к любому API (GET/POST/PUT/DELETE) с заголовками "
            "и телом. Для интеграций и REST-сервисов.",
            {"method": "GET|POST|...", "url": "адрес",
             "headers": "(опц.) объект заголовков", "params": "(опц.) query-параметры",
             "json": "(опц.) JSON-тело", "body": "(опц.) сырое тело"},
            tool_http_request,
        ))
        self.add(Tool(
            "wikipedia",
            "Краткая энциклопедическая справка из Wikipedia по теме.",
            {"query": "тема/заголовок статьи", "lang": "(опц.) язык, по умолч. ru"},
            tool_wikipedia,
        ))
        self.add(Tool(
            "exchange_rate",
            "Курсы валют и конвертация сумм (без ключей).",
            {"base": "базовая валюта (USD,EUR,RUB,...)",
             "target": "(опц.) целевые валюты через запятую",
             "amount": "(опц.) сумма для конвертации"},
            tool_exchange_rate,
        ))
        self.add(Tool(
            "define",
            "Определение английского слова (словарь).",
            {"word": "слово на английском"},
            tool_define,
        ))
        self.add(Tool(
            "system_info",
            "Сводка о ХОСТ-машине: ОС, CPU, RAM, GPU (VRAM и загрузка).",
            {},
            tool_system_info,
        ))
        self.add(Tool(
            "open_url",
            "Открыть веб-страницу или поиск в браузере на ПК. Используй это для "
            "«открой вкладку/сайт/страницу с X». Если знаешь адрес — передай url; "
            "иначе передай query и откроется поиск.",
            {"url": "адрес страницы (если известен)",
             "query": "поисковый запрос (если адрес неизвестен)"},
            tool_open_url,
        ))
        self.add(Tool(
            "list_dir",
            "Перечислить файлы в папке на ПК (для анализа проекта/каталога). "
            "Затем читай нужные файлы через windows.read_file.",
            {"path": "путь к папке (поддержка %USERPROFILE%, ~)"},
            tool_list_dir,
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
