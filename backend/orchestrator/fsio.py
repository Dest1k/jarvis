# -*- coding: utf-8 -*-
"""
fsio.py — единый слой файлового ввода-вывода JARVIS v2.0 (инженерный стандарт).

Зачем отдельный модуль:
    Система пишет файлы из десятка мест (память, журнал инцидентов, навыки,
    конфиги), исполняется и на Linux (контейнеры/WSL2), и на Windows (хост).
    Любой «плавающий» перевод строки (CRLF) или неявная кодировка консоли
    превращается в '#!/usr/bin/env bash\\r' и часы отладки. Поэтому ВСЕ записи
    текстовых артефактов идут через этот модуль со ЖЁСТКИМИ гарантиями:

        • кодировка — строго UTF-8 (без BOM), независимо от локали процесса;
        • переводы строк — строго LF (CRLF/CR нормализуются при записи);
        • запись АТОМАРНА: содержимое пишется во временный файл рядом с целью
          и подменяется os.replace() — читатель никогда не видит полуфайла
          (критично для JSON-журналов, которые читаются конкурентно).

Исключение из LF-нормализации — намеренно Windows-специфичные артефакты
(*.bat/*.ps1); для них есть write_text(..., keep_eol=True).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.fsio")

ENCODING = "utf-8"


def normalize_eol(text: str) -> str:
    """Нормализовать все переводы строк к LF (CRLF и одиночные CR → LF)."""
    if not text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text(path: Path | str, content: str, *, keep_eol: bool = False) -> Path:
    """
    Атомарно записать текст в UTF-8 с LF-окончаниями.

    keep_eol=True отключает нормализацию переводов строк (для файлов,
    которым CRLF нужен по природе: .bat, .ps1). Кодировка UTF-8 — всегда.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = content if keep_eol else normalize_eol(content)
    # newline="" — отдаём байты переводов строк как есть, без трансляции
    # платформой (иначе Windows молча превратит LF обратно в CRLF).
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=ENCODING, newline="") as fh:
            fh.write(data)
        os.replace(tmp_name, p)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def write_json(path: Path | str, obj: Any, *, indent: int = 2) -> Path:
    """Атомарно сериализовать объект в JSON (UTF-8, LF, без ASCII-эскейпов)."""
    payload = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    return write_text(path, payload)


def read_text(path: Path | str) -> str:
    """Прочитать текст в UTF-8 (толерантно к битым байтам)."""
    return Path(path).read_text(encoding=ENCODING, errors="replace")


def read_json(path: Path | str, default: Any = None) -> Any:
    """Прочитать JSON; при отсутствии/повреждении файла вернуть default."""
    p = Path(path)
    try:
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding=ENCODING))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Не удалось прочитать JSON %s: %s", p, exc)
        return default


def resolve_writable_dir(preferred: str, *fallbacks: str) -> Path:
    """
    Найти первый каталог из списка, куда реально можно писать (пробная запись).
    Используется памятью/журналами: том может быть не смонтирован — тогда
    деградируем в /tmp, но не падаем.
    """
    for candidate in (preferred, *fallbacks):
        try:
            p = Path(candidate)
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_probe"
            probe.write_text("ok", encoding=ENCODING)
            probe.unlink(missing_ok=True)
            return p
        except OSError:
            continue
    return Path(tempfile.gettempdir())
