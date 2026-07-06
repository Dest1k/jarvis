# -*- coding: utf-8 -*-
"""Background idle diagnostics loop for JARVIS.

Runs only after user inactivity and when GPU guard allows it. By default it is
safe diagnostics-only. Branch/test self-healing cycles require
JARVIS_SELF_HEAL_ENABLE=1 and still report for operator review.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from .cluster import cluster_router
from .network_resilience import diagnose_and_optionally_recover

log = logging.getLogger("jarvis.idle")
HostExec = Callable[[str], Awaitable[dict[str, Any]]]
Broadcast = Callable[[dict[str, Any]], Awaitable[None]]

ERROR_PATTERNS = [
    re.compile(r"RuntimeError: UVA is not available", re.I),
    re.compile(r"CUDA out of memory|OutOfMemoryError", re.I),
    re.compile(r"Name or service not known|All connection attempts failed", re.I),
    re.compile(r"MCP .*not.*(start|connect|initialize)|No module named mcp_server", re.I),
    re.compile(r"Traceback \(most recent call last\)", re.I),
]

@dataclass
class IdleState:
    active: bool = False
    last_user_activity: float = 0.0
    last_iteration: float = 0.0
    iteration: int = 0
    last_anomaly: str = ""
    last_action: str = ""
    self_heal_enabled: bool = False

class BackgroundIdleLoop:
    def __init__(self, *, host_exec: HostExec | None, broadcast: Broadcast | None, gpu_guard: Any | None = None) -> None:
        self.host_exec = host_exec
        self.broadcast = broadcast
        self.gpu_guard = gpu_guard
        self.idle_after_sec = float(os.environ.get("JARVIS_IDLE_AFTER_SEC", "45"))
        self.interval_sec = float(os.environ.get("JARVIS_IDLE_INTERVAL_SEC", "90"))
        self.self_heal_enabled = os.environ.get("JARVIS_SELF_HEAL_ENABLE", "0") == "1"
        self.repo_path = os.environ.get("JARVIS_REPO_PATH", ".")
        self._state = IdleState(last_user_activity=time.time(), self_heal_enabled=self.self_heal_enabled)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._active_turns = 0

    def status(self) -> dict[str, Any]:
        return asdict(self._state) | {"active_turns": self._active_turns}

    def mark_user_activity(self, active: bool = True) -> None:
        self._state.last_user_activity = time.time()
        if active:
            self._active_turns += 1
        else:
            self._active_turns = max(0, self._active_turns - 1)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="jarvis-idle-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass

    async def _emit(self, message: dict[str, Any]) -> None:
        if self.broadcast is not None:
            await self.broadcast({"type": "idle", "ts": time.time(), **message})

    async def _run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.interval_sec)
            if self._active_turns > 0:
                continue
            if time.time() - self._state.last_user_activity < self.idle_after_sec:
                continue
            if self.gpu_guard is not None and getattr(self.gpu_guard, "throttle_delay", lambda: 0.0)() >= 1.0:
                await self._emit({"level": "warn", "message": "Idle loop paused: GPU guard reports pressure."})
                continue
            try:
                await self.iterate()
            except Exception as exc:  # noqa: BLE001
                log.exception("Idle loop iteration failed")
                await self._emit({"level": "err", "message": f"Idle loop error: {exc}"})

    async def iterate(self) -> None:
        self._state.active = True
        self._state.iteration += 1
        self._state.last_iteration = time.time()
        await self._emit({"level": "info", "message": "Background diagnostics started."})
        try:
            await cluster_router.refresh_health()
            await self._emit({"level": "info", "message": "Cluster health refreshed.", "cluster": cluster_router.status()})
            if self.host_exec is not None:
                anomalies = await self._scan_runtime_logs()
                if anomalies:
                    self._state.last_anomaly = anomalies[0]
                    await self._emit({"level": "warn", "message": anomalies[0][:400]})
                    await self._self_heal(anomalies[0])
                net = await diagnose_and_optionally_recover(self.host_exec)
                if not net.get("container_http", {}).get("ok"):
                    await self._emit({"level": "warn", "message": "Researcher network probe failed; safe recovery path evaluated.", "network": net})
        finally:
            self._state.active = False

    async def _scan_runtime_logs(self) -> list[str]:
        if self.host_exec is None:
            return []
        cmd = "docker logs --tail 240 jarvis-backend 2>&1 && docker logs --tail 160 jarvis-vllm-qwen 2>&1"
        res = await self.host_exec(cmd)
        text = res.get("out", "")
        return [line.strip() for line in text.splitlines() if any(p.search(line) for p in ERROR_PATTERNS)][:10]

    async def _self_heal(self, anomaly: str) -> None:
        if not self.self_heal_enabled or self.host_exec is None:
            await self._emit({"level": "info", "message": "Self-heal is diagnostic-only. Set JARVIS_SELF_HEAL_ENABLE=1 to allow branch/test cycles."})
            return
        branch = "fix/jarvis-auto-" + str(int(time.time()))
        steps = [
            f'git -C "{self.repo_path}" checkout -B "{branch}"',
            f'python -m compileall "{self.repo_path}/backend"',
            f'cmd /c "cd /d {self.repo_path} && docker compose -f wsl/docker-compose.agents.yml --env-file wsl/.env config"',
        ]
        results = []
        for cmd in steps:
            r = await self.host_exec(cmd)
            results.append({"cmd": cmd, "ok": r.get("ok"), "out": (r.get("out") or "")[-1200:]})
            if not r.get("ok"):
                break
        ok = all(r.get("ok") for r in results)
        self._state.last_action = f"self-heal branch {branch}: {'ok' if ok else 'failed'}"
        msg = (f"Sir, I detected an anomaly and prepared branch {branch}. Tests passed. Shall I open/merge the pull request?" if ok else f"Sir, I detected an anomaly, but autonomous validation failed on branch {branch}.")
        await self._emit({"level": "ok" if ok else "err", "message": msg, "anomaly": anomaly, "results": results})
