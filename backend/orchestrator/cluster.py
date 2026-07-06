# -*- coding: utf-8 -*-
"""cluster.py — asynchronous LAN/mesh worker abstraction for JARVIS.

This is agent-level distribution, not model tensor splitting. The head node keeps
control and can offload independent sub-agent work to worker nodes exposing a
local OpenAI-compatible vLLM endpoint or a small HTTP worker API. Transports are
plain URLs, so LAN IPs and mesh/VPN addresses are both supported.
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
    weight: int = 1
    enabled: bool = True
    last_ok: bool = False
    last_seen: float = 0.0
    last_error: str = ""


class ClusterRouter:
    def __init__(self) -> None:
        self.nodes = self._load_nodes()
        self.timeout = float(os.environ.get("JARVIS_CLUSTER_TIMEOUT", "90"))
        self.health_timeout = float(os.environ.get("JARVIS_CLUSTER_HEALTH_TIMEOUT", "4"))
        self._rr = 0

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
                        weight=int(item.get("weight") or 1),
                        enabled=bool(item.get("enabled", True)),
                    ))
            return out
        except Exception:
            # Compact fallback: name=url;role=model, separated by commas.
            out = []
            for part in raw.split(","):
                if "=" in part:
                    name, url = part.split("=", 1)
                    out.append(WorkerNode(name=name.strip(), base_url=url.strip().rstrip("/")))
            return out

    def status(self) -> dict[str, Any]:
        return {"enabled": bool(self.nodes), "nodes": [asdict(n) for n in self.nodes]}

    async def probe(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.health_timeout) as cli:
            async def one(node: WorkerNode) -> None:
                if not node.enabled:
                    return
                try:
                    r = await cli.get(f"{node.base_url}/health")
                    node.last_ok = r.status_code < 500
                    node.last_seen = time.time()
                    node.last_error = "" if node.last_ok else f"HTTP {r.status_code}"
                except Exception as exc:  # noqa: BLE001
                    node.last_ok = False
                    node.last_error = str(exc)[:160]
            await asyncio.gather(*(one(n) for n in self.nodes))
        return self.status()

    def choose(self, role: str = "general") -> Optional[WorkerNode]:
        candidates = [n for n in self.nodes if n.enabled and n.last_ok and (n.role in (role, "general") or role == "general")]
        if not candidates:
            candidates = [n for n in self.nodes if n.enabled and n.last_ok]
        if not candidates:
            return None
        self._rr = (self._rr + 1) % len(candidates)
        return candidates[self._rr]

    async def chat(self, messages: list[dict[str, Any]], *, role: str = "general", model: str = "", max_tokens: int = 1024) -> dict[str, Any]:
        node = self.choose(role)
        if node is None:
            return {"ok": False, "error": "no healthy cluster worker"}
        body = {
            "model": model or node.model or os.environ.get("JARVIS_CLUSTER_DEFAULT_MODEL", "qwen-coder"),
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                r = await cli.post(f"{node.base_url.rstrip('/')}/v1/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
            return {"ok": True, "node": node.name, "content": data["choices"][0]["message"].get("content", "")}
        except Exception as exc:  # noqa: BLE001
            node.last_ok = False
            node.last_error = str(exc)[:160]
            return {"ok": False, "node": node.name, "error": str(exc)}


cluster_router = ClusterRouter()
