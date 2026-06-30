# -*- coding: utf-8 -*-
"""
llm.py — низкоуровневый клиент к vLLM-инстансам JARVIS-OS и утилиты бюджета
контекста.

Здесь сосредоточена ВСЯ работа с «мозгами» системы — двумя локальными моделями:
    • Qwen2.5-Coder-14B (диспетчер + кодер)  → http://vllm-qwen-coder:8001/v1
    • UI-TARS-2B        (контроллер GUI/ОС)  → http://vllm-ui-tars:8002/v1

Модуль намеренно НЕ зависит от langchain/langgraph: прямые httpx-вызовы к
OpenAI-совместимому API vLLM устойчивы к смене версий и дают полный контроль
над потоковой выдачей и бюджетом токенов (критично, чтобы не упереться в
контекстное окно модели и не спровоцировать OOM по KV-кэшу).
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

# --------------------------------------------------------------------------- #
# Адреса и имена моделей (совпадают с docker-compose / server.py)
# --------------------------------------------------------------------------- #
QWEN_URL = os.environ.get("JARVIS_QWEN_URL", "http://vllm-qwen-coder:8001/v1")
UITARS_URL = os.environ.get("JARVIS_UITARS_URL", "http://vllm-ui-tars:8002/v1")
QWEN_MODEL = os.environ.get("JARVIS_QWEN_MODEL_NAME", "qwen-coder")
UITARS_MODEL = os.environ.get("JARVIS_UITARS_MODEL_NAME", "ui-tars")

# Зрение / GUI-контроль (скриншот → действие). Это ОДНА точка, через которую
# идёт визуальное управление экраном — кто именно «смотрит», задаётся профилем:
#   • СОЛО-режим (единый мозг Gemma-4, мультимодальная): VISION_* указывают на
#     сам диспетчер — Gemma видит скриншот и сама решает, куда кликнуть/что ввести.
#     Отдельная vLLM-модель под GUI больше не нужна.
#   • Классические профили (с UI-TARS): VISION_* указывают на инстанс UI-TARS.
# По умолчанию (если профиль не задал переменные) — UI-TARS, ради обратной
# совместимости со старыми конфигурациями.
VISION_URL = os.environ.get("JARVIS_VISION_URL", UITARS_URL)
VISION_MODEL = os.environ.get("JARVIS_VISION_MODEL", UITARS_MODEL)

# Контекстные окна (берём из тех же переменных, что и compose). Держим бюджет
# ВХОДА строго ниже окна, оставляя место под генерацию — иначе vLLM отвергнет
# запрос (prompt + max_tokens > max_model_len) либо переполнит KV-кэш.
QWEN_MAX_LEN = int(os.environ.get("JARVIS_QWEN_MAX_LEN", "16384"))
UITARS_MAX_LEN = int(os.environ.get("JARVIS_UITARS_MAX_LEN", "8192"))

# Сколько токенов резервируем под ответ модели (генерацию).
QWEN_OUTPUT_RESERVE = int(os.environ.get("JARVIS_QWEN_OUTPUT_RESERVE", "3072"))
# Итоговый бюджет ВХОДНОГО контекста для Qwen-агента.
AGENT_INPUT_BUDGET = max(2048, QWEN_MAX_LEN - QWEN_OUTPUT_RESERVE)


# --------------------------------------------------------------------------- #
# Оценка токенов (без внешнего токенайзера — консервативная эвристика)
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """
    Грубая, но НАМЕРЕННО завышенная оценка числа токенов.

    Точный токенайзер Qwen недоступен в backend-контейнере без лишних
    зависимостей, поэтому считаем по символам с коэффициентом, безопасным
    для смеси русского и английского/кода (~3 символа на токен). Завышение
    безопаснее занижения: лучше отрезать лишнее, чем переполнить окно.
    """
    if not text:
        return 0
    return int(len(text) / 3.0) + 1


def messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Суммарная оценка токенов списка сообщений (+overhead на роли/разметку)."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            # мультимодальный контент (текст + изображения)
            for part in content:
                if part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""))
                else:
                    total += 512  # грубый вес изображения-плейсхолдера
        else:
            total += estimate_tokens(str(content))
        total += 4  # overhead на служебные токены роли
    return total


# --------------------------------------------------------------------------- #
# Вызовы к vLLM
# --------------------------------------------------------------------------- #
async def chat(
    messages: list[dict[str, Any]],
    *,
    base_url: str = QWEN_URL,
    model: str = QWEN_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    timeout: float = 180.0,
    stop: Optional[list[str]] = None,
) -> str:
    """Неблокирующий НЕ-стриминговый чат-комплишен. Возвращает текст ответа."""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if stop:
        body["stop"] = stop
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(f"{base_url}/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"] or ""


async def chat_stream(
    messages: list[dict[str, Any]],
    *,
    base_url: str = QWEN_URL,
    model: str = QWEN_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: Optional[float] = None,
) -> AsyncIterator[str]:
    """
    Потоковый чат-комплишен: асинхронно отдаёт дельты текста по мере генерации.
    Используется для финального ответа агента (живая «печать» в чате).
    """
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=timeout) as cli:
        async with cli.stream("POST", f"{base_url}/chat/completions", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


# --------------------------------------------------------------------------- #
# Извлечение JSON из ответа модели (устойчивое к «болтовне» вокруг JSON)
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> Optional[dict[str, Any]]:
    """
    Достать ПЕРВЫЙ валидный JSON-объект из текста модели.

    Модель просят отвечать чистым JSON, но на практике она иногда оборачивает
    его в ```json … ``` или добавляет пояснения. Сначала пробуем код-фенсы,
    затем — сканирование сбалансированных фигурных скобок.
    """
    if not text:
        return None
    text = text.strip()

    # 1) ```json … ``` или ``` … ```
    if "```" in text:
        fenced = text.split("```")
        for i in range(1, len(fenced), 2):
            block = fenced[i]
            if block.startswith("json"):
                block = block[4:]
            obj = _try_load_object(block)
            if obj is not None:
                return obj

    # 2) Прямой разбор
    obj = _try_load_object(text)
    if obj is not None:
        return obj

    # 3) Сканирование сбалансированных скобок
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        obj = _try_load_object(candidate)
                        if obj is not None:
                            return obj
                        break
        start = text.find("{", start + 1)
    return None


def _try_load_object(s: str) -> Optional[dict[str, Any]]:
    s = s.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None
