# -*- coding: utf-8 -*-
"""
cluster.py — async agent-level distributed compute abstraction.

This deliberately rejects synchronous tensor/model splitting over LAN. Remote
nodes are autonomous workers exposing OpenAI-compatible vLLM endpoints and a
small /health contract; JARVIS offloads whole sub-agent jobs asynchronously.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

import httpx


@dataclass
class WorkerNode:
    name: str
    base_url: str
    model: str
    role: str = "general"
    transport: str = "lan"  # lan | tailscale | wireguard | custom
    weight: int = 1
    enabled: bool = True
    last_ok: bool = False
    latency_ms: Optional[float] = None
    last_error: str = ""
    updated_at: float = 0.0


class ClusterRouter:
    def __init__(self) -> None:
        self.nodes: list[WorkerNode] = self._load_nodes()
        self.timeout = float(os.environ.get("JARVIS_CLUSTER_TIMEOUT", "120"))
        self._rr = 0

    def _load_nodes(self) -> list[WorkerNode]:
        raw = os.environ.get("JARVIS_CLUSTER_NODES", "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            nodes = data if isinstance(data, list) else data.get("nodes", [])
            out: list[WorkerNode] = []
            for n in nodes:
                out.append(WorkerNode(
                    name=str(n.get("name") or n.get("host") or "worker"),
                    base_url=str(n["base_url"]).rstrip("/"),
                    model=str(n.get("model", "worker-model")),
                    role=str(n.get("role", "general")),
                    transport=str(n.get("transport", "lan")),
                    weight=int(n.get("weight", 1)),
                    enabled=bool(n.get("enabled", True)),
                ))
            return out
        except Exception:
            return []

    def status(self) -> dict[str, Any]:
        return {"enabled": bool(self.nodes), "nodes": [asdict(n) for n in self.nodes]}

    async def refresh_health(self) -> dict[str, Any]:
        async def probe(node: WorkerNode) -> None:
            if not node.enabled:
                return
            start = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=5) as cli:
                    r = await cli.get(node.base_url.replace("/v1", "") + "/health")
                node.last_ok = r.status_code == 200
                node.latency_ms = round((time.perf_counter() - start) * 1000, 1)
                node.last_error = "" if node.last_ok else f"HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                node.last_ok = False
                node.latency_ms = None
                node.last_error = str(exc)[:200]
            node.updated_at = time.time()
        await asyncio.gather(*(probe(n) for n in self.nodes))
        return self.status()

    def choose(self, role: str = "general") -> Optional[WorkerNode]:
        candidates = [n for n in self.nodes if n.enabled and n.last_ok and n.role in (role, "general")]
        if not candidates:
            candidates = [n for n in self.nodes if n.enabled and n.last_ok]
        if not candidates:
            return None
        candidates.sort(key=lambda n: ((n.latency_ms or 999999) / max(n.weight, 1)))
        self._rr = (self._rr + 1) % len(candidates)
        return candidates[self._rr]

    async def offload_chat(self, messages: list[dict[str, Any]], *, role: str = "general", max_tokens: int = 1024) -> dict[str, Any]:
        if not any(n.last_ok for n in self.nodes):
            await self.refresh_health()
        node = self.choose(role)
        if node is None:
            return {"ok": False, "error": "No healthy cluster worker available."}
        body = {"model": node.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2, "stream": False}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                r = await cli.post(f"{node.base_url}/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
            return {"ok": True, "node": asdict(node), "content": data["choices"][0]["message"].get("content", "")}
        except Exception as exc:  # noqa: BLE001
            node.last_ok = False
            node.last_error = str(exc)[:200]
            return {"ok": False, "node": asdict(node), "error": str(exc)}


cluster_router = ClusterRouter()
