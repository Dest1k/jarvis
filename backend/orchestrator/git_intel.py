# -*- coding: utf-8 -*-
"""
git_intel.py — автономный Git-интеллект JARVIS v2.0.

Возможности:
    • scan     — разведка репозитория: `git branch -a`, активная ветка,
                 последние коммиты каждой ветки (свежесть/направление работ);
    • evaluate — сравнение активной ветки с соседней: diff-оценка + анализ
                 диспетчер-моделью, какие УЛУЧШЕННЫЕ блоки кода существуют в
                 соседней ветке и отсутствуют в активной;
    • port     — БЕЗОПАСНЫЙ перенос выбранных файлов из ветки-донора: всегда
                 в СВЕЖУЮ staging-ветку `jarvis/port-<ts>` (активная ветка и
                 main не трогаются), фиксация отдельным коммитом.

Гарантии безопасности:
    • Никаких force-операций, никакого reset --hard, никаких пушей (пуш — это
      отдельный `git_push` RPC-моста со своим HITL-гейтом и запретом main).
    • Перенос выполняется только в новую staging-ветку; исходное состояние
      остаётся нетронутым в прежней ветке.
    • На Windows-хосте команды `git checkout <ветка> -- <пути>` дополнительно
      проходят HITL-гейт моста (паттерн 'git checkout -- ' — деструктивный).

Исполнение команд абстрагировано (runner): реальный хост через RPC-мост или
sandbox-контейнер для репозиториев в /workspace — модуль не знает разницы.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional

from . import llm

log = logging.getLogger("jarvis.git")

# Runner: команда → (returncode | None, объединённый вывод)
GitRunner = Callable[[str], Awaitable[tuple[Optional[int], str]]]

MAX_DIFF_CHARS = 12000       # потолок diff, уходящего в модель
MAX_LOG_LINES = 12
_PROTECTED = {"main", "master"}

_BRANCH_RE = re.compile(r"^[\w./-]{1,120}$")
_PATH_RE = re.compile(r"^[\w./ -]{1,240}$")


def _safe_branch(name: str) -> bool:
    return bool(name) and ".." not in name and bool(_BRANCH_RE.match(name))


def _safe_path(p: str) -> bool:
    return bool(p) and ".." not in p and not p.startswith(("/", "\\")) \
        and bool(_PATH_RE.match(p))


class GitIntelligence:
    """Разведка веток, diff-оценка и безопасный перенос улучшений."""

    def __init__(self, repo: str, run: GitRunner) -> None:
        self.repo = repo
        self._run = run

    async def _git(self, args: str) -> tuple[Optional[int], str]:
        rc, out = await self._run(f'git -C "{self.repo}" {args}')
        return rc, (out or "").strip()

    # ------------------------------------------------------------------ #
    # scan — разведка веток
    # ------------------------------------------------------------------ #
    async def scan(self) -> dict[str, Any]:
        rc, inside = await self._git("rev-parse --is-inside-work-tree")
        if rc != 0 or "true" not in inside:
            return {"ok": False,
                    "content": f"'{self.repo}' не является git-репозиторием "
                               f"({inside[:200]})."}
        await self._git("fetch --all --prune --quiet")   # best-effort свежесть
        _, current = await self._git("rev-parse --abbrev-ref HEAD")
        _, branches_raw = await self._git("branch -a --format=%(refname:short)")
        branches = [b.strip() for b in branches_raw.splitlines()
                    if b.strip() and "HEAD" not in b]

        lines = [f"Активная ветка: {current}", "Обнаруженные ветки (git branch -a):"]
        for b in branches[:30]:
            _, head = await self._git(
                f'log -1 --format="%h %ad %s" --date=short "{b}" --')
            lines.append(f"  • {b}: {head[:120]}")
        _, status = await self._git("status --short")
        if status:
            lines.append("Незафиксированные изменения:\n" + status[:800])
        return {"ok": True, "content": "\n".join(lines),
                "data": {"current": current, "branches": branches}}

    # ------------------------------------------------------------------ #
    # evaluate — diff-оценка соседней ветки моделью
    # ------------------------------------------------------------------ #
    async def evaluate(self, donor: str) -> dict[str, Any]:
        if not _safe_branch(donor):
            return {"ok": False, "content": f"Недопустимое имя ветки: '{donor}'."}
        _, current = await self._git("rev-parse --abbrev-ref HEAD")
        rc, stat = await self._git(f'diff --stat HEAD..."{donor}" --')
        if rc != 0:
            return {"ok": False,
                    "content": f"Не удалось сравнить с '{donor}': {stat[:300]}"}
        if not stat:
            return {"ok": True,
                    "content": f"Ветка '{donor}' не содержит изменений "
                               f"относительно '{current}'."}
        _, diff = await self._git(f'diff HEAD..."{donor}" --')
        diff_bounded = diff[:MAX_DIFF_CHARS]
        truncated = len(diff) > MAX_DIFF_CHARS

        analysis = await self._analyze_diff(current, donor, stat, diff_bounded)
        body = (f"Сравнение '{current}' ← '{donor}':\n{stat[:1200]}\n\n"
                f"АНАЛИЗ ДИСПЕТЧЕРА:\n{analysis}")
        if truncated:
            body += "\n(diff усечён для анализа — переносите файлы точечно)"
        return {"ok": True, "content": body,
                "data": {"current": current, "donor": donor, "stat": stat}}

    async def _analyze_diff(self, current: str, donor: str,
                            stat: str, diff: str) -> str:
        """Спросить диспетчер-модель, какие блоки донора стоит перенести."""
        messages = [
            {"role": "system", "content": (
                "Ты — модуль Git-интеллекта JARVIS. Тебе дан diff между активной "
                "веткой и веткой-донором. Определи, какие изменения донора — "
                "это УЛУЧШЕНИЯ, отсутствующие в активной ветке (оптимизации, "
                "исправления ошибок, повышение надёжности), а какие — шум или "
                "регресс. Ответь ПО-РУССКИ, кратко и структурированно:\n"
                "1) СТОИТ ПЕРЕНЕСТИ: список файлов с одной строкой обоснования;\n"
                "2) НЕ ПЕРЕНОСИТЬ: список файлов с причиной;\n"
                "3) РИСКИ: на что смотреть при переносе (конфликты, зависимости)."
            )},
            {"role": "user", "content":
                f"Активная ветка: {current}\nВетка-донор: {donor}\n\n"
                f"Статистика:\n{stat[:1500]}\n\nDiff:\n{diff}"},
        ]
        try:
            return (await llm.chat(messages, temperature=0.2, max_tokens=900,
                                   timeout=150)).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM-анализ diff не удался: %s", exc)
            return (f"(Модель недоступна для анализа: {exc}. "
                    "Опирайтесь на статистику diff выше.)")

    # ------------------------------------------------------------------ #
    # port — безопасный перенос файлов из донора в staging-ветку
    # ------------------------------------------------------------------ #
    async def port(self, donor: str, paths: list[str],
                   message: str = "") -> dict[str, Any]:
        if not _safe_branch(donor):
            return {"ok": False, "content": f"Недопустимое имя ветки: '{donor}'."}
        clean_paths = [p.strip() for p in (paths or []) if p and p.strip()]
        if not clean_paths:
            return {"ok": False,
                    "content": "Не указаны пути для переноса (paths)."}
        bad = [p for p in clean_paths if not _safe_path(p)]
        if bad:
            return {"ok": False,
                    "content": f"Отклонены небезопасные пути: {', '.join(bad[:5])}."}
        if donor.split("/")[-1] in _PROTECTED:
            # Донором main быть МОЖЕТ (частый случай: подтянуть фикс из main),
            # но предупредим в журнале — перенос всё равно идёт в staging.
            log.info("Донор — защищённая ветка '%s' (перенос в staging).", donor)

        _, current = await self._git("rev-parse --abbrev-ref HEAD")
        rc, dirty = await self._git("status --porcelain")
        if rc == 0 and dirty:
            return {"ok": False,
                    "content": "В рабочем дереве незафиксированные изменения — "
                               "сначала закоммитьте или отложите их (git stash). "
                               "Перенос по-джентльменски не топчет чужую работу."}

        staging = f"jarvis/port-{int(time.time())}"
        steps: list[str] = []

        rc, out = await self._git(f'checkout -b "{staging}"')
        steps.append(f"checkout -b {staging}: rc={rc}")
        if rc != 0:
            return {"ok": False,
                    "content": f"Не удалось создать staging-ветку: {out[:300]}"}

        quoted = " ".join(f'"{p}"' for p in clean_paths)
        rc, out = await self._git(f'checkout "{donor}" -- {quoted}')
        steps.append(f"checkout {donor} -- <paths>: rc={rc}")
        if rc != 0:
            # откат: вернуться на исходную ветку, staging остаётся пустой
            await self._git(f'checkout "{current}"')
            return {"ok": False,
                    "content": f"Перенос не удался (файлы не тронуты): {out[:400]}"}

        msg = message.strip() or (
            f"JARVIS: перенос оптимизаций из '{donor}' ({len(clean_paths)} файлов)")
        await self._git("add -A")
        rc, out = await self._git(f'commit -m "{msg}"')
        steps.append(f"commit: rc={rc}")
        if rc != 0:
            await self._git(f'checkout "{current}"')
            return {"ok": False,
                    "content": f"Нечего фиксировать или commit не удался: {out[:300]}"}

        # Возвращаем оператора на исходную ветку — staging ждёт ревью/пуша.
        await self._git(f'checkout "{current}"')
        return {"ok": True,
                "content": (f"Перенос выполнен, сэр: файлы {', '.join(clean_paths[:8])} "
                            f"из '{donor}' зафиксированы в ветке '{staging}'. "
                            f"Активная ветка '{current}' не тронута. Публикация — "
                            f"отдельной командой git_push (с подтверждением)."),
                "data": {"staging": staging, "steps": steps}}
