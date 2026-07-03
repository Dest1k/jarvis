# -*- coding: utf-8 -*-
"""
models.py — pydantic v2 модели когнитивного ядра.

Pydantic v2 выбран намеренно: компилируемая (pydantic-core на Rust) валидация и
сериализация → минимум CPU-оверхеда между БД, контекстным окном и фронтендом
(требование «serialization efficiency»). model_dump_json() отдаёт быстрый JSON
для WebSocket-стриминга без ручной сборки словарей.

Все модели — «плоские» и человекочитаемые, 1:1 к таблицам schema.sql, чтобы
DB Explorer/Admin Browser могли редактировать их без трансляции.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

CognitiveState = Literal["IDLE", "THINKING", "TESTING", "SUSPENDED", "RECOVERING"]
Role = Literal["admin", "operator", "auditor"]
Visibility = Literal["private", "team", "federated"]
KnowledgeKind = Literal["skill", "rule", "knowledge", "plugin", "pattern"]


class AgentStateModel(BaseModel):
    state: CognitiveState = "IDLE"
    prev_state: Optional[str] = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    active_goal: Optional[str] = None
    active_user: Optional[str] = None
    updated_at: float = 0.0


class SettingModel(BaseModel):
    key: str
    value: Any
    value_type: Literal["string", "int", "float", "bool", "json", "prompt"] = "string"
    category: str = "general"
    description: Optional[str] = None
    is_active: bool = True
    file_default: Optional[Any] = None
    version: int = 1


class FileAttachment(BaseModel):
    id: str
    file_name: str
    storage_ref: str
    sha256: str
    mime_type: Optional[str] = None
    size_bytes: int = 0
    message_id: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    owner_id: Optional[str] = None
    origin: Literal["user", "agent"] = "user"
    ingest_status: Literal["pending", "parsing", "embedding", "ready", "failed"] = "pending"
    token_count: Optional[int] = None
    chunk_count: Optional[int] = None


class KnowledgeNode(BaseModel):
    id: str
    kind: KnowledgeKind = "rule"
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    owner_id: Optional[str] = None
    visibility: Visibility = "team"
    importance: float = 0.5
    decay_score: float = 1.0
    use_count: int = 0
    status: Literal["draft", "active", "archived", "rejected"] = "draft"
    critic_verdict: Optional[Literal["approved", "rejected", "pending"]] = None
    version: int = 1


class EpisodicEntry(BaseModel):
    id: str
    decision_trace: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    step_index: Optional[int] = None
    entry_type: Literal["thought", "hypothesis", "action", "observation",
                        "sandbox_output", "error", "success", "failure"] = "thought"
    content: str
    used_knowledge_ids: list[str] = Field(default_factory=list)
    used_file_ids: list[str] = Field(default_factory=list)
    outcome: Optional[Literal["success", "failure", "partial", "unknown"]] = None


class HealthSnapshot(BaseModel):
    component: str
    status: Literal["healthy", "degraded", "down", "unknown"]
    detail: Optional[str] = None
    suggested_fix: Optional[str] = None
    fix_applied: bool = False


class ProjectTask(BaseModel):
    id: str
    plan_id: str
    title: str
    assignee: Literal["orchestrator", "researcher", "coder", "critic"] = "orchestrator"
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["todo", "in_progress", "blocked", "done", "failed"] = "todo"
    progress: float = 0.0
    order_index: int = 0
