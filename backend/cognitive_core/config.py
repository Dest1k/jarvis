# -*- coding: utf-8 -*-
"""
config.py — унифицированное управление конфигурацией JARVIS OS.

Приоритет (жёсткий, по требованию):

    БД (system_settings_and_prompts, is_active=1)  ПЕРЕКРЫВАЕТ  файловый конфиг.

Порядок разрешения значения ключа:
    1. Активная запись в БД (is_active=1) — высший приоритет (hot-swap из UI).
    2. Файловый конфиг (JSON) — стартовый fallback.
    3. Явный default, переданный в вызове.

Кэш значений держится с коротким TTL, чтобы правки из «Settings & Prompts
Editor» подхватывались в рантайме без рестарта, но без обращения к БД на
каждый вызов (эффективность). Инвалидация — по set_setting()/явно.

Промпт-шаблоны (value_type='prompt') разрешаются тем же механизмом: файловый
дефолт можно перекрыть «живой» версией из БД и откатить одной кнопкой.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from . import db

# Файловый конфиг-fallback (стартовые значения). Реальные файлы проекта:
# wsl/.env, mcp_servers.json и т.п. — здесь единый JSON-слой для когнитивного ядра.
CONFIG_FILE = Path(os.environ.get(
    "JARVIS_CONFIG_FILE",
    str(Path(os.environ.get("JARVIS_CORE_DIR", "./.jarvis_core")) / "config.json")))

_CACHE_TTL = float(os.environ.get("JARVIS_CONFIG_CACHE_TTL", "5.0"))
_cache: dict[str, tuple[float, Any]] = {}
_file_cache: Optional[dict[str, Any]] = None


def _load_file() -> dict[str, Any]:
    global _file_cache
    if _file_cache is None:
        try:
            _file_cache = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) \
                if CONFIG_FILE.exists() else {}
        except (OSError, json.JSONDecodeError):
            _file_cache = {}
    return _file_cache


def _coerce(value: str, value_type: str) -> Any:
    try:
        if value_type == "int":
            return int(value)
        if value_type == "float":
            return float(value)
        if value_type == "bool":
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if value_type in ("json", "prompt"):
            return json.loads(value) if value_type == "json" else value
    except (ValueError, json.JSONDecodeError):
        return value
    return value


async def get_setting(key: str, default: Any = None) -> Any:
    """
    Разрешить значение ключа: БД (is_active) → файл → default.
    Кэшируется на _CACHE_TTL секунд.
    """
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    value: Any = None
    resolved = False

    row = await db.query_one(
        "SELECT value, value_type FROM system_settings_and_prompts "
        "WHERE key = ? AND is_active = 1", (key,))
    if row is not None:
        value = _coerce(row["value"], row["value_type"])
        resolved = True

    if not resolved:
        file_cfg = _load_file()
        if key in file_cfg:
            value = file_cfg[key]
            resolved = True

    if not resolved:
        value = default

    _cache[key] = (now, value)
    return value


async def set_setting(key: str, value: Any, *, value_type: str = "string",
                      category: str = "general", description: str = "",
                      actor: str = "local-admin", reason: str = "") -> dict[str, Any]:
    """
    Установить/обновить значение в БД (перекрывает файл). Пишется в аудит и
    инвалидирует кэш → hot-swap без рестарта.
    """
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) \
        if value_type in ("json",) else str(value)
    file_default = _load_file().get(key)
    existing = await db.query_one(
        "SELECT key, version FROM system_settings_and_prompts WHERE key = ?", (key,))
    if existing:
        result = await db.mutate_with_audit(
            table="system_settings_and_prompts", row_id=key, op="update", pk_col="key",
            sql="UPDATE system_settings_and_prompts SET value=?, value_type=?, "
                "category=?, description=?, is_active=1, version=version+1, "
                "updated_by=?, updated_at=unixepoch('subsec') WHERE key=?",
            params=(raw, value_type, category, description, actor, key),
            actor=actor, actor_kind="human", reason=reason or "settings update")
    else:
        result = await db.mutate_with_audit(
            table="system_settings_and_prompts", row_id=key, op="create", pk_col="key",
            sql="INSERT INTO system_settings_and_prompts "
                "(key,value,value_type,category,description,is_active,file_default,updated_by) "
                "VALUES (?,?,?,?,?,1,?,?)",
            params=(key, raw, value_type, category, description,
                    json.dumps(file_default, ensure_ascii=False) if file_default is not None else None,
                    actor),
            actor=actor, actor_kind="human", reason=reason or "settings create")
    _cache.pop(key, None)
    return result


async def reset_to_file_default(key: str, actor: str = "local-admin") -> dict[str, Any]:
    """
    Сбросить ключ к файловому дефолту: деактивировать DB-override (is_active=0).
    После этого get_setting снова берёт значение из файла.
    """
    result = await db.mutate_with_audit(
        table="system_settings_and_prompts", row_id=key, op="update", pk_col="key",
        sql="UPDATE system_settings_and_prompts SET is_active=0, "
            "updated_by=?, updated_at=unixepoch('subsec') WHERE key=?",
        params=(actor, key), actor=actor, actor_kind="human",
        reason="reset to file default")
    _cache.pop(key, None)
    return result


async def get_prompt(name: str, file_default: str = "") -> str:
    """Разрешить промпт-шаблон (DB override → файл → переданный дефолт)."""
    return await get_setting(f"prompt.{name}", default=file_default)


def invalidate_cache() -> None:
    _cache.clear()
    global _file_cache
    _file_cache = None
