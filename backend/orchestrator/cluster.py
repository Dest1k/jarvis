# -*- coding: utf-8 -*-
"""cluster.py — adaptive LAN/mesh worker abstraction for JARVIS.

This is agent-level distribution, not model tensor splitting. The head node keeps
control and can offload independent sub-agent work to worker nodes exposing a
local OpenAI-compatible vLLM endpoint. Transports are plain URLs, so LAN IPs,
Tailscale names and WireGuard addresses are all supported.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

import httpx


@dataclass
class WorkerNode:
    name: str
    base_url: str
    role: str = "general"
    model: str = ""
    transport: str = "lan"
    weight: int = 1
    enabled: bool = True
    last_ok: bool = False
    last_seen: float = 0.0
    last_error: str = ""
    last_latency_ms: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    inflight: int = 0

    def score(self, role: str = "general") -> float:
        if not self.enabled or not self.last_ok:
            return -1.0
        role_bonus = 1.5 if self.role in (role, "general") or role == "general" else 0.75
        latency = max(self.last_latency_ms or 250.0, 25.0)
        error_penalty = 1.0 + min(self.failure_count, 10) * 0.25
        busy_penalty = 1.0 + self.inflight * 0.7
        return (max(self.weight, 1) * role_bonus * 1000.0) / (latency * error_penalty * busy_penalty)


class ClusterRouter:
    def __init__(self) -> None:
        self.nodes = self._load_nodes()
        self.timeout = float(os.environ.get("JARVIS_CLUSTER_TIMEOUT", "90"))
        self.health_timeout = float(os.environ.get("JARVIS_CLUSTER_HEALTH_TIMEOUT", "4"))
        self.default_model = os.environ.get("JARVIS_CLUSTER_DEFAULT_MODEL", "dispatcher")
        self._lock = asyncio.Lock()

    @staticmethod
    def _load_nodes() -> list[WorkerNode]:
        raw = os.environ.get("JARVIS_CLUSTER_NODES", "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            out: list[WorkerNode] = []
            for item in data if isinstance(data, list) else []:
                if isinstance(item, dict) and item.get("base_url"):
                    out.append(WorkerNode(
                        name=str(item.get("name") or item["base_url"]),
                        base_url=str(item["base_url"]).rstrip("/"),
                        role=str(item.get("role") or "general"),
                        model=str(item.get("model") or ""),
                        transport=str(item.get("transport") or "lan"),
                        weight=int(item.get("weight") or 1),
                        enabled=bool(item.get("enabled", True)),
                    ))
            return out
        except Exception:
            out: list[WorkerNode] = []
            for part in raw.split(","):
                if "=" in part:
                    name, url = part.split("=", 1)
                    out.append(WorkerNode(name=name.strip(), base_url=url.strip().rstrip("/")))
            return out

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.nodes),
            "healthy": sum(1 for n in self.nodes if n.enabled and n.last_ok),
            "nodes": [asdict(n) for n in self.nodes],
        }

    async def refresh_health(self) -> dict[str, Any]:
        return await self.probe()

    async def probe(self) -> dict[str, Any]:
        if not self.nodes:
            return self.status()
        async with httpx.AsyncClient(timeout=self.health_timeout) as cli:
            async def one(node: WorkerNode) -> None:
                if not node.enabled:
                    return
                started = time.perf_counter()
                try:
                    # Prefer /health, fall back to OpenAI-compatible /v1/models.
                    r = await cli.get(f"{node.base_url}/health")
                    if r.status_code == 404:
                        r = await cli.get(f"{node.base_url}/v1/models")
                    node.last_ok = r.status_code < 500
                    node.last_seen = time.time()
                    node.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
                    node.last_error = "" if node.last_ok else f"HTTP {r.status_code}"
                except Exception as exc:  # noqa: BLE001
                    node.last_ok = False
                    node.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
                    node.last_error = str(exc)[:160]
            await asyncio.gather(*(one(n) for n in self.nodes))
        return self.status()

    async def choose(self, role: str = "general") -> Optional[WorkerNode]:
        async with self._lock:
            candidates = [n for n in self.nodes if n.enabled and n.last_ok]
            if not candidates:
                return None
            ranked = sorted(candidates, key=lambda n: n.score(role), reverse=True)
            best = ranked[0]
            if best.score(role) < 0:
                return None
            best.inflight += 1
            return best

    async def chat(self, messages: list[dict[str, Any]], *, role: str = "general", model: str = "", max_tokens: int = 1024) -> dict[str, Any]:
        node = await self.choose(role)
        if node is None:
            return {"ok": False, "error": "no healthy cluster worker"}
        body = {
            "model": model or node.model or self.default_model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                r = await cli.post(f"{node.base_url.rstrip('/')}/v1/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
            node.last_ok = True
            node.last_seen = time.time()
            node.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            node.success_count += 1
            node.last_error = ""
            return {"ok": True, "node": node.name, "latency_ms": node.last_latency_ms, "content": data["choices"][0]["message"].get("content", "")}
        except Exception as exc:  # noqa: BLE001
            node.last_ok = False
            node.failure_count += 1
            node.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            node.last_error = str(exc)[:160]
            return {"ok": False, "node": node.name, "error": str(exc)}
        finally:
            node.inflight = max(0, node.inflight - 1)

    async def offload_chat(self, messages: list[dict[str, Any]], *, role: str = "general", model: str = "", max_tokens: int = 1024) -> dict[str, Any]:
        """Compatibility alias used by sub-agent orchestration."""
        if not any(n.last_ok for n in self.nodes):
            await self.refresh_health()
        return await self.chat(messages, role=role, model=model, max_tokens=max_tokens)


cluster_router = ClusterRouter()
