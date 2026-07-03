# -*- coding: utf-8 -*-
"""
skills.py — «кузница навыков» JARVIS v2.0 (динамическое создание инструментов).

Идея:
    Если агент раз за разом выполняет одну и ту же рутину руками (одинаковая
    последовательность инструментов), это сигнал: рутину пора СКОМПИЛИРОВАТЬ
    в постоянный CLI-скрипт. Кузница:

        1. НАБЛЮДАЕТ за ходами агента (observe): нормализует последовательность
           действий хода в сигнатуру рутины и считает повторения в
           `./.jarvis_core/routines.json`;
        2. при пороге повторов ПОДСКАЗЫВАЕТ агенту скомпилировать навык;
        3. КОМПИЛИРУЕТ (compile_skill): кладёт самодостаточный CLI-скрипт в
           `./.jarvis_core/tools/<имя>.<py|sh>` (UTF-8, LF, shebang, `--help`)
           и регистрирует его в центральном индексе:
               • `./.jarvis_core/tools/INDEX.md`        — человекочитаемый;
               • `./.jarvis_core/tools/skills_index.json` — машиночитаемый;
        4. ИСПОЛНЯЕТ (run_skill) навык в изолированном sandbox-контейнере —
           скомпилированный код по-прежнему не касается хоста напрямую.

Все записи — через fsio (атомарно, UTF-8, LF): скрипт, рождённый на Windows-
хосте, обязан исполняться в Linux-контейнере без единого CRLF-сюрприза.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import fsio
from .incidents import CORE_DIR

log = logging.getLogger("jarvis.skills")

TOOLS_DIR = CORE_DIR / "tools"
INDEX_MD = TOOLS_DIR / "INDEX.md"
INDEX_JSON = TOOLS_DIR / "skills_index.json"
ROUTINES_PATH = CORE_DIR / "routines.json"

# Порог повторов рутины, после которого предлагается компиляция навыка.
SUGGEST_AFTER = int(os.environ.get("JARVIS_SKILL_SUGGEST_AFTER", "3"))

_LANG_SPEC = {
    "python": {"ext": ".py", "shebang": "#!/usr/bin/env python3", "run": "python3"},
    "bash":   {"ext": ".sh", "shebang": "#!/usr/bin/env bash", "run": "bash"},
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    return s[:48]


class SkillForge:
    """Наблюдение за рутинами + компиляция и исполнение CLI-навыков."""

    def __init__(self) -> None:
        self._routines: dict[str, dict[str, Any]] = fsio.read_json(
            ROUTINES_PATH, default={}) or {}
        self._index: dict[str, dict[str, Any]] = fsio.read_json(
            INDEX_JSON, default={}) or {}

    # ------------------------------------------------------------------ #
    # 1. Наблюдение за рутинами
    # ------------------------------------------------------------------ #
    def observe(self, actions: list[str]) -> Optional[dict[str, Any]]:
        """
        Зафиксировать последовательность действий завершённого хода.

        Возвращает {"routine", "count"} когда рутина достигла порога повторов
        и для неё ещё не скомпилирован навык — сигнал агенту предложить
        компиляцию. Одношаговые ходы не считаем рутиной.
        """
        meaningful = [a for a in actions if a and a != "answer"]
        if len(meaningful) < 2:
            return None
        signature = "→".join(meaningful)
        entry = self._routines.setdefault(
            signature, {"count": 0, "first_ts": time.time(), "suggested": False})
        entry["count"] += 1
        entry["last_ts"] = time.time()
        try:
            fsio.write_json(ROUTINES_PATH, self._routines)
        except OSError as exc:
            log.warning("Не удалось сохранить журнал рутин: %s", exc)
        if entry["count"] >= SUGGEST_AFTER and not entry["suggested"]:
            entry["suggested"] = True
            try:
                fsio.write_json(ROUTINES_PATH, self._routines)
            except OSError:
                pass
            return {"routine": signature, "count": entry["count"]}
        return None

    # ------------------------------------------------------------------ #
    # 2. Компиляция навыка
    # ------------------------------------------------------------------ #
    def compile_skill(self, *, name: str, description: str, code: str,
                      language: str = "python",
                      usage: str = "") -> dict[str, Any]:
        """
        Сохранить самодостаточный CLI-скрипт навыка и обновить индексы.

        Требования к скрипту обеспечиваются на записи: UTF-8, LF, shebang,
        маркер кодировки для python. Возвращает {ok, path|error}.
        """
        slug = _slug(name)
        if not _NAME_RE.match(slug):
            return {"ok": False,
                    "error": f"Недопустимое имя навыка '{name}' "
                             "(нужно: латиница/цифры/подчёркивания, от 3 символов)."}
        lang = language.strip().lower()
        spec = _LANG_SPEC.get(lang)
        if spec is None:
            return {"ok": False,
                    "error": f"Язык '{language}' не поддерживается "
                             f"(доступно: {', '.join(_LANG_SPEC)})."}
        body = fsio.normalize_eol(code or "").strip()
        if not body:
            return {"ok": False, "error": "Пустой код навыка."}

        # Гарантируем shebang и (для python) явный маркер кодировки.
        if not body.startswith("#!"):
            header = spec["shebang"] + "\n"
            if lang == "python":
                header += "# -*- coding: utf-8 -*-\n"
            body = header + body
        if not body.endswith("\n"):
            body += "\n"

        path = TOOLS_DIR / f"{slug}{spec['ext']}"
        fsio.write_text(path, body)
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass

        self._index[slug] = {
            "name": slug,
            "file": path.name,
            "language": lang,
            "description": (description or "").strip()[:300],
            "usage": (usage or f"{spec['run']} {path.name} --help").strip()[:200],
            "created_ts": self._index.get(slug, {}).get("created_ts", time.time()),
            "updated_ts": time.time(),
        }
        self._write_indexes()
        log.info("Навык скомпилирован: %s (%s)", slug, path)
        return {"ok": True, "path": str(path), "name": slug}

    def _write_indexes(self) -> None:
        fsio.write_json(INDEX_JSON, self._index)
        lines = [
            "# Индекс навыков JARVIS (.jarvis_core/tools)",
            "",
            "Автоматически скомпилированные CLI-рутины. Файл генерируется "
            "кузницей навыков — правки вносите через `skill_compile`.",
            "",
            "| Навык | Файл | Язык | Назначение | Запуск |",
            "|-------|------|------|-----------|--------|",
        ]
        for slug in sorted(self._index):
            it = self._index[slug]
            desc = it["description"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {slug} | `{it['file']}` | {it['language']} "
                         f"| {desc} | `{it['usage']}` |")
        lines.append("")
        fsio.write_text(INDEX_MD, "\n".join(lines))

    # ------------------------------------------------------------------ #
    # 3. Каталог и исполнение
    # ------------------------------------------------------------------ #
    def list_skills(self) -> list[dict[str, Any]]:
        return [self._index[k] for k in sorted(self._index)]

    def get(self, name: str) -> Optional[dict[str, Any]]:
        return self._index.get(_slug(name))

    async def run_skill(self, name: str, args: list[str],
                        timeout: int = 120) -> dict[str, Any]:
        """
        Исполнить навык в ИЗОЛИРОВАННОМ sandbox-контейнере.

        Скрипт передаётся base64 (кавычки/юникод безопасны), исполняется
        интерпретатором языка навыка; аргументы — как есть после '--'.
        """
        item = self.get(name)
        if item is None:
            return {"ok": False,
                    "content": f"Навык '{name}' не найден. "
                               f"Доступны: {', '.join(sorted(self._index)) or '(нет)'}."}
        path = TOOLS_DIR / item["file"]
        try:
            script = fsio.read_text(path)
        except OSError as exc:
            return {"ok": False, "content": f"Не удалось прочитать навык: {exc}"}

        runner = _LANG_SPEC[item["language"]]["run"]
        script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        arg_str = " ".join(_shell_quote(a) for a in (args or []))
        inner = (
            f"mkdir -p /workspace/.skills && cd /workspace/.skills; "
            f"echo {script_b64} | base64 -d > {item['file']}; "
            f"timeout {int(timeout)} {runner} {item['file']} {arg_str} 2>&1"
        )
        from . import dockerapi
        sandbox = os.environ.get("JARVIS_SANDBOX_CONTAINER", "jarvis-sandbox")
        try:
            rc, output = await dockerapi.exec_run(
                sandbox, ["bash", "-lc", inner], timeout=timeout + 10)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False,
                    "content": f"Sandbox недоступен для запуска навыка: {exc}"}
        body = (output or "").rstrip() or "(навык выполнен, вывод пуст)"
        return {"ok": rc == 0, "content": f"{body}\n[код возврата: {rc}]",
                "data": {"returncode": rc, "skill": item["name"]}}


def _shell_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


# Синглтон кузницы (живёт на всё время работы backend; конструктор только
# читает индексы с диска — безопасен при недоступном каталоге).
forge = SkillForge()
