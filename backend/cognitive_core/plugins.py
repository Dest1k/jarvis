# -*- coding: utf-8 -*-
"""
plugins.py — архитектура горячих подключаемых навыков-плагинов JARVIS OS.

Все инструменты/интеграции (SSH, Ansible, Docker, monitoring API, …) —
плагины с ЕДИНЫМ интерфейсом. И пользователь, и сам агент могут добавлять новые
плагины (с обязательной Critic-валидацией перед активацией), НЕ трогая ядро.

Свойства:
    • Стандартный контракт SkillPlugin (manifest + async run + опц. validate).
    • Уровень опасности danger_level → определяет, нужен ли HITL перед запуском
      (согласуется с autonomy_level роли пользователя).
    • Реестр обнаруживает плагины из каталога ./.jarvis_core/plugins/*.py и из
      записей semantic_knowledge_graph (kind='plugin'), но АКТИВИРУЕТ только
      прошедшие Critic (critic_verdict='approved').
    • Горячая замена: register()/unregister() без рестарта ядра.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PluginManifest:
    name: str                                   # уникальное имя (snake_case)
    version: str = "0.1.0"
    description: str = ""
    category: str = "sysadmin"                  # sysadmin|automation|monitoring|assistant
    # 0=безопасно (чтение) … 3=критично (меняет прод). >=2 требует HITL.
    danger_level: int = 0
    parameters: dict[str, str] = field(default_factory=dict)  # имя→описание для промпта
    author: str = "agent"


class SkillPlugin(abc.ABC):
    """Базовый контракт плагина-навыка. Реализуйте manifest и run()."""

    @property
    @abc.abstractmethod
    def manifest(self) -> PluginManifest: ...

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        """Выполнить навык. Возврат: {ok: bool, content: str, data?: ...}."""

    async def validate(self) -> dict[str, Any]:
        """
        Самопроверка перед активацией (опц.). Critic-Agent дополнительно проверит
        безопасность/логику. По умолчанию — успех.
        """
        return {"ok": True, "notes": "no self-validation"}


class PluginRegistry:
    """Горячий реестр плагинов с учётом Critic-валидации и danger-уровня."""

    def __init__(self) -> None:
        self._plugins: dict[str, SkillPlugin] = {}
        self._approved: set[str] = set()

    def register(self, plugin: SkillPlugin, *, critic_approved: bool = False) -> None:
        name = plugin.manifest.name
        self._plugins[name] = plugin
        if critic_approved:
            self._approved.add(name)

    def approve(self, name: str) -> None:
        """Пометить плагин как прошедший Critic-валидацию → доступен к запуску."""
        if name in self._plugins:
            self._approved.add(name)

    def unregister(self, name: str) -> None:
        self._plugins.pop(name, None)
        self._approved.discard(name)

    def get(self, name: str) -> Optional[SkillPlugin]:
        return self._plugins.get(name)

    def list_active(self) -> list[PluginManifest]:
        """Манифесты только одобренных Critic плагинов (их «видит» оркестратор)."""
        return [p.manifest for n, p in self._plugins.items() if n in self._approved]

    def requires_hitl(self, name: str, autonomy_level: int) -> bool:
        """
        Нужен ли HITL перед запуском: опасные плагины (danger_level>=2) требуют
        подтверждения, если уровень автономии пользователя недостаточен.
        Порог: разрешаем без HITL только если autonomy_level > danger_level.
        """
        p = self._plugins.get(name)
        if p is None:
            return True
        return p.manifest.danger_level >= 2 and autonomy_level <= p.manifest.danger_level

    async def run(self, name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        plugin = self._plugins.get(name)
        if plugin is None:
            return {"ok": False, "content": f"Плагин '{name}' не найден."}
        if name not in self._approved:
            return {"ok": False,
                    "content": f"Плагин '{name}' не активирован (ждёт Critic-валидации)."}
        try:
            return await plugin.run(args, ctx)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "content": f"Плагин '{name}' упал: {exc}"}


# Синглтон-реестр процесса.
registry = PluginRegistry()
