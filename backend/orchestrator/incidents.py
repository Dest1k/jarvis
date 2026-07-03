# -*- coding: utf-8 -*-
"""
incidents.py — журнал решённых инцидентов JARVIS v2.0 (эпистемическое логирование).

Назначение:
    Каждая перехваченная ошибка исполнения (traceback, ненулевой код возврата,
    отказ внешнего сервиса) — это знание. Журнал каталогизирует пары
    «сигнатура ошибки → проверенное решение» в локальном JSON-реестре
    `./.jarvis_core/resolved_incidents.json`, чтобы:

        1. при ПОВТОРНОЙ встрече похожей ошибки самокорректирующийся цикл
           агента сразу получал подсказку с готовым рецептом (lookup);
        2. система накапливала опыт между сессиями и рестартами контейнера;
        3. оператор мог ревизовать, ЧЕМУ научилась система (файл человекочитаем).

Сопоставление — по нормализованной сигнатуре ошибки (класс исключения /
головная строка stderr, очищенная от изменчивых частей: пути, адреса, числа)
плюс keyword-пересечение. Без эмбеддингов — оффлайн и ноль VRAM.

Формат записи:
    {id, ts, tool, signature, error, resolution, occurrences}

Запись через fsio — атомарно, UTF-8, LF.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import fsio

log = logging.getLogger("jarvis.incidents")

# Корневой каталог ядра v2 (журналы, навыки). По умолчанию — рядом с рабочей
# директорией процесса (./.jarvis_core), переопределяется JARVIS_CORE_DIR
# (в контейнере backend монтируется на том данных — переживает рестарты).
CORE_DIR = Path(os.environ.get("JARVIS_CORE_DIR", "./.jarvis_core"))
LEDGER_PATH = CORE_DIR / "resolved_incidents.json"

MAX_INCIDENTS = 800          # потолок реестра (старые вытесняются)
MAX_ERROR_CHARS = 1500       # сколько текста ошибки храним
_MIN_SCORE = 2.0             # порог релевантности для lookup


def _normalize_signature(error_text: str) -> str:
    """
    Свести текст ошибки к устойчивой сигнатуре.

    Берём последнюю содержательную строку (в traceback именно она несёт класс
    и суть исключения), затем вычищаем изменчивые фрагменты: hex-адреса,
    длинные числа, пути, UUID — чтобы «одна и та же» ошибка с разными путями
    давала одну сигнатуру.
    """
    lines = [l.strip() for l in (error_text or "").strip().splitlines() if l.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    tail = re.sub(r"0x[0-9a-fA-F]+", "<addr>", tail)
    tail = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+", "<path>", tail)
    tail = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  "<uuid>", tail)
    tail = re.sub(r"\b\d{4,}\b", "<num>", tail)
    return tail[:300].lower()


def _keywords(text: str) -> set[str]:
    return {w for w in re.split(r"[^\wа-яёА-ЯЁ]+", (text or "").lower()) if len(w) > 3}


class IncidentLedger:
    """Локальный JSON-реестр «ошибка → решение» с поиском по сигнатуре."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or LEDGER_PATH
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        data = fsio.read_json(self.path, default=[])
        self._items = data if isinstance(data, list) else []

    def _save(self) -> None:
        try:
            fsio.write_json(self.path, self._items)
        except OSError as exc:
            log.warning("Не удалось сохранить журнал инцидентов: %s", exc)

    # --- запись --------------------------------------------------------- #
    def record(self, *, tool: str, error: str, resolution: str,
               context: str = "") -> dict[str, Any]:
        """
        Занести решённый инцидент. Если сигнатура уже известна — обновляем
        решение и наращиваем счётчик встреч (свежий рецепт ценнее старого).
        """
        signature = _normalize_signature(error)
        for item in self._items:
            if item["tool"] == tool and item["signature"] == signature:
                item["resolution"] = resolution.strip()[:1200]
                item["occurrences"] = int(item.get("occurrences", 1)) + 1
                item["ts"] = time.time()
                self._save()
                return item
        item = {
            "id": str(uuid.uuid4())[:8],
            "ts": time.time(),
            "tool": tool,
            "signature": signature,
            "error": (error or "").strip()[:MAX_ERROR_CHARS],
            "context": (context or "").strip()[:400],
            "resolution": resolution.strip()[:1200],
            "occurrences": 1,
        }
        self._items.append(item)
        if len(self._items) > MAX_INCIDENTS:
            self._items = self._items[-MAX_INCIDENTS:]
        self._save()
        log.info("Инцидент занесён в журнал: tool=%s sig=%.80s", tool, signature)
        return item

    # --- поиск ---------------------------------------------------------- #
    def lookup(self, tool: str, error: str) -> Optional[dict[str, Any]]:
        """
        Найти наиболее релевантный решённый инцидент для свежей ошибки.
        Сигнатурное совпадение перевешивает; keyword-пересечение добирает
        близкие случаи (та же суть, другой текст).
        """
        if not self._items:
            return None
        signature = _normalize_signature(error)
        err_words = _keywords(error)
        best: tuple[float, Optional[dict[str, Any]]] = (0.0, None)
        for item in self._items:
            score = 0.0
            if item["signature"] and item["signature"] == signature:
                score += 10.0
            overlap = len(err_words & _keywords(item.get("error", "")))
            score += min(overlap, 8) * 0.5
            if item["tool"] == tool:
                score += 1.0
            score += min(int(item.get("occurrences", 1)), 5) * 0.2
            if score > best[0]:
                best = (score, item)
        return best[1] if best[0] >= _MIN_SCORE else None

    def all(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(reversed(self._items[-limit:]))

    def clear(self) -> int:
        n = len(self._items)
        self._items = []
        self._save()
        return n
