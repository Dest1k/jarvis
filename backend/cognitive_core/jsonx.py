# -*- coding: utf-8 -*-
"""
jsonx.py — устойчивое извлечение JSON из ответов LLM (для суб-агентов ядра).

Reasoning-модели (Gemma 4) «думают» перед ответом, иногда в тегах
<think>…</think>, иногда прозой со скобками. Наивный `re.search(r"\\{.*\\}")`
(жадный) хватает от ПЕРВОЙ `{` рассуждения до ПОСЛЕДНЕЙ `}` ответа → битый JSON →
суб-агент молча уходит в дефолт (Critic «одобряет» без LLM, decompose падает в
эвристику). Здесь ищем сбалансированные объекты/массивы и берём последний валидный.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_REASONING_RE = re.compile(
    r"<(think|thought|thinking|reasoning|analysis)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _balanced(text: str, open_ch: str, close_ch: str) -> list[str]:
    """Все верхнеуровневые сбалансированные фрагменты open_ch…close_ch (с учётом строк)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != open_ch:
            i += 1
            continue
        depth = 0
        in_str = esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    out.append(text[i:j + 1])
                    break
            j += 1
        i = j + 1
    return out


def first_obj(raw: str) -> Optional[dict[str, Any]]:
    """Последний валидный JSON-объект из ответа модели (reasoning отброшен)."""
    if not raw:
        return None
    text = _REASONING_RE.sub(" ", raw)
    found: Optional[dict[str, Any]] = None
    for frag in _balanced(text, "{", "}"):
        try:
            obj = json.loads(frag)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            found = obj
    return found


def first_array(raw: str) -> Optional[list[Any]]:
    """Последний валидный JSON-массив из ответа модели (reasoning отброшен)."""
    if not raw:
        return None
    text = _REASONING_RE.sub(" ", raw)
    found: Optional[list[Any]] = None
    for frag in _balanced(text, "[", "]"):
        try:
            arr = json.loads(frag)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(arr, list):
            found = arr
    return found
