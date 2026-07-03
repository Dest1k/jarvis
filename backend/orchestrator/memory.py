# -*- coding: utf-8 -*-
"""
memory.py — память JARVIS-OS: постоянная (долговременная) + менеджер
оперативного контекста диалога.

Зачем:
    Локальные модели (Qwen 16k, UI-TARS 8k) имеют ОГРАНИЧЕННОЕ контекстное окно.
    Бесконтрольный рост истории диалога переполняет окно и провоцирует отказ
    vLLM / рост KV-кэша → риск OOM. Здесь реализованы два уровня памяти:

    1. LongTermMemory  — факты/заметки на диске (переживают рестарт контейнера),
       с простым keyword-поиском (без эмбеддингов — оффлайн, без лишней VRAM).
    2. ConversationManager — оперативная история диалога с АВТО-СУММАРИЗАЦИЕЙ:
       старые реплики периодически «сбрасываются» в сжатую сводку (и в
       долговременную память), а сырые сообщения удаляются из активного окна.
       Это и есть запрошенный «сброс контекста куда-нибудь + очистка
       оперативного контекста у моделей».

Хранилище — простой JSON на смонтированном томе (JARVIS_MEMORY_DIR), с
безопасным фолбэком в /tmp, если каталог недоступен.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import fsio
from .llm import estimate_tokens

log = logging.getLogger("jarvis.memory")

# Пишущий каталог памяти (том → /tmp как фолбэк). Все записи идут через fsio:
# атомарно (никогда не видно полуфайла), строго UTF-8 + LF.
MEMORY_DIR = fsio.resolve_writable_dir(
    os.environ.get("JARVIS_MEMORY_DIR", "/data/memory"), "/tmp/jarvis-memory")


# --------------------------------------------------------------------------- #
# Долговременная память (факты/заметки)
# --------------------------------------------------------------------------- #
class LongTermMemory:
    """Постоянное хранилище заметок с keyword-поиском."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (MEMORY_DIR / "longterm.json")
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._items = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Не удалось загрузить долговременную память: %s", exc)
            self._items = []

    def _save(self) -> None:
        try:
            fsio.write_json(self.path, self._items)
        except OSError as exc:
            log.warning("Не удалось сохранить долговременную память: %s", exc)

    def save(self, text: str, tags: Optional[list[str]] = None,
             kind: str = "note") -> dict[str, Any]:
        """Сохранить заметку. Возвращает созданную запись."""
        item = {
            "id": str(uuid.uuid4())[:8],
            "ts": time.time(),
            "kind": kind,             # note | summary | fact
            "text": text.strip(),
            "tags": tags or [],
        }
        self._items.append(item)
        # держим разумный потолок, чтобы файл не разрастался бесконечно
        if len(self._items) > 2000:
            self._items = self._items[-2000:]
        self._save()
        return item

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Найти релевантные заметки простым пересечением слов."""
        q_words = _tokenize(query)
        if not q_words:
            # без запроса — последние записи
            return list(reversed(self._items[-k:]))
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in self._items:
            words = _tokenize(item["text"]) | set(item.get("tags", []))
            overlap = len(q_words & words)
            if overlap:
                # лёгкий бонус свежести
                recency = 1.0 / (1.0 + (time.time() - item["ts"]) / 86400.0)
                scored.append((overlap + 0.3 * recency, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:k]]

    def all(self) -> list[dict[str, Any]]:
        return list(reversed(self._items))

    def clear(self) -> int:
        n = len(self._items)
        self._items = []
        self._save()
        return n


def _tokenize(text: str) -> set[str]:
    return {w for w in re.split(r"[^\wа-яёА-ЯЁ]+", (text or "").lower()) if len(w) > 2}


# --------------------------------------------------------------------------- #
# Оперативная память диалога (с авто-суммаризацией)
# --------------------------------------------------------------------------- #
Summarizer = Callable[[str], Awaitable[str]]


class Conversation:
    """Одна диалоговая сессия: сводка прошлого + недавние реплики."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.summary: str = ""                       # сжатая сводка «старого»
        self.messages: list[dict[str, Any]] = []     # недавние реплики {role, content, ts}

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "messages": self.messages}

    @classmethod
    def from_dict(cls, session_id: str, data: dict[str, Any]) -> "Conversation":
        c = cls(session_id)
        c.summary = data.get("summary", "")
        c.messages = data.get("messages", [])
        return c


class ConversationManager:
    """
    Менеджер оперативного контекста всех сессий.

    Политика бюджета:
        • Активное окно (summary + recent messages) держим ниже мягкого порога
          по токенам. При превышении — самые старые реплики суммируются в
          `summary` (и копия — в долговременную память), затем удаляются.
        • build_context() собирает финальный список сообщений для модели,
          гарантированно укладываясь в переданный бюджет.
    """

    def __init__(self, longterm: LongTermMemory,
                 soft_budget_tokens: int = 6000,
                 keep_recent: int = 8) -> None:
        self.longterm = longterm
        self.soft_budget = soft_budget_tokens
        self.keep_recent = keep_recent
        self.path = MEMORY_DIR / "conversations.json"
        self._sessions: dict[str, Conversation] = {}
        self._load()

    # --- персистентность ---------------------------------------------------
    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for sid, data in raw.items():
                    self._sessions[sid] = Conversation.from_dict(sid, data)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Не удалось загрузить диалоги: %s", exc)

    def _save(self) -> None:
        try:
            raw = {sid: c.to_dict() for sid, c in self._sessions.items()}
            fsio.write_json(self.path, raw)
        except OSError as exc:
            log.warning("Не удалось сохранить диалоги: %s", exc)

    # --- доступ ------------------------------------------------------------
    def get(self, session_id: str) -> Conversation:
        if session_id not in self._sessions:
            self._sessions[session_id] = Conversation(session_id)
        return self._sessions[session_id]

    def add(self, session_id: str, role: str, content: str) -> None:
        conv = self.get(session_id)
        conv.messages.append({"role": role, "content": content, "ts": time.time()})
        self._save()

    def reset(self, session_id: str, *, keep_summary: bool = False) -> None:
        """
        Очистить ОПЕРАТИВНЫЙ контекст сессии (что и просили: «очистить контекст
        у моделей»). По умолчанию стирает и сводку; keep_summary сохраняет её.
        Долговременная память при этом НЕ трогается.
        """
        conv = self.get(session_id)
        conv.messages = []
        if not keep_summary:
            conv.summary = ""
        self._save()

    # --- авто-суммаризация (сброс контекста) ------------------------------
    async def maybe_summarize(self, session_id: str, summarizer: Summarizer) -> bool:
        """
        Если активное окно превысило бюджет — сжать старые реплики в сводку и
        перенести их в долговременную память. Возвращает True, если сжатие
        произошло.
        """
        conv = self.get(session_id)
        active_tokens = estimate_tokens(conv.summary) + sum(
            estimate_tokens(m["content"]) for m in conv.messages
        )
        if active_tokens <= self.soft_budget or len(conv.messages) <= self.keep_recent:
            return False

        # отделяем «старое» (всё, кроме последних keep_recent)
        old = conv.messages[: -self.keep_recent]
        recent = conv.messages[-self.keep_recent :]
        transcript = "\n".join(
            f"{'Пользователь' if m['role'] == 'user' else 'JARVIS'}: {m['content']}"
            for m in old
        )
        prior = f"Предыдущая сводка:\n{conv.summary}\n\n" if conv.summary else ""
        try:
            new_summary = await summarizer(
                prior
                + "Новые реплики для добавления в сводку:\n"
                + transcript
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Суммаризация не удалась (%s) — обрезаю по бюджету.", exc)
            new_summary = conv.summary

        conv.summary = new_summary.strip()
        conv.messages = recent
        # копия сводки — в долговременную память, как «слепок» контекста
        self.longterm.save(conv.summary, tags=["context_summary", session_id],
                           kind="summary")
        self._save()
        log.info("Контекст сессии '%s' сжат: %d старых реплик → сводку.",
                 session_id, len(old))
        return True

    async def flush(self, session_id: str, summarizer: Summarizer) -> bool:
        """Принудительно сжать ВСЮ историю в сводку (ручной сброс из дашборда)."""
        conv = self.get(session_id)
        if not conv.messages:
            return False
        transcript = "\n".join(
            f"{'Пользователь' if m['role'] == 'user' else 'JARVIS'}: {m['content']}"
            for m in conv.messages
        )
        prior = f"Предыдущая сводка:\n{conv.summary}\n\n" if conv.summary else ""
        try:
            conv.summary = (await summarizer(prior + "Реплики:\n" + transcript)).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("Flush-суммаризация не удалась: %s", exc)
            return False
        conv.messages = []
        self.longterm.save(conv.summary, tags=["context_summary", session_id],
                           kind="summary")
        self._save()
        return True

    # --- сборка контекста под бюджет --------------------------------------
    def build_context(self, session_id: str, budget_tokens: int) -> list[dict[str, str]]:
        """
        Собрать сообщения диалога (без system) под жёсткий бюджет токенов:
        сводка (если есть) + максимально возможный хвост недавних реплик.
        """
        conv = self.get(session_id)
        out: list[dict[str, str]] = []
        used = 0

        if conv.summary:
            block = {"role": "system",
                     "content": f"[Сводка предыдущего диалога]\n{conv.summary}"}
            tok = estimate_tokens(block["content"])
            if tok < budget_tokens:
                out.append(block)
                used += tok

        # добавляем реплики с конца, пока влезают
        tail: list[dict[str, str]] = []
        for m in reversed(conv.messages):
            tok = estimate_tokens(m["content"]) + 4
            if used + tok > budget_tokens:
                break
            tail.append({"role": m["role"], "content": m["content"]})
            used += tok
        tail.reverse()
        out.extend(tail)
        return out
