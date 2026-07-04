# -*- coding: utf-8 -*-
"""
parallel.py — утилиты ОГРАНИЧЕННОГО параллелизма для когнитивного ядра.

vLLM обслуживает запросы континуальным батчингом: несколько одновременных
запросов к «мозгу» он складывает в один батч и считает эффективнее, чем строго
по очереди. Значит, НЕзависимую работу (эмбеддинг множества чанков, независимые
задачи плана, ревью пачки правил) нужно запускать в 2+ потока — но с ПОТОЛКОМ,
чтобы не захлебнуться (не переполнить KV-кэш и не выесть VRAM/сеть).

`bounded_map` — параллельно применить async-функцию к списку с семафором,
СОХРАНЯЯ порядок результатов. `concurrency` — прочитать потолок из env.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def concurrency(env_key: str, default: int) -> int:
    """Потолок параллелизма из переменной окружения (>=1), с безопасным дефолтом."""
    try:
        return max(1, int(os.environ.get(env_key, str(default))))
    except (TypeError, ValueError):
        return max(1, default)


async def bounded_map(func: Callable[[T], Awaitable[R]], items: Sequence[T], *,
                      limit: int) -> list[R]:
    """
    Применить `func` ко всем `items` параллельно, но не более `limit` одновременно.
    Порядок результатов совпадает с порядком входа (как у asyncio.gather).
    """
    items = list(items)
    if not items:
        return []
    if limit <= 1 or len(items) == 1:
        # Вырожденный случай — без накладных расходов на семафор/таски.
        return [await func(x) for x in items]
    sem = asyncio.Semaphore(limit)

    async def _one(x: T) -> R:
        async with sem:
            return await func(x)

    return await asyncio.gather(*(_one(x) for x in items))
