# -*- coding: utf-8 -*-
"""Background idle diagnostics / self-healing loop for JARVIS.

Runs only after user inactivity and when GPU guard allows it. The loop is
safe-by-default: diagnostics run automatically, while branch/test self-healing
requires JARVIS_SELF_HEAL_ENABLE=1. Even then it prepares evidence and a staging
branch/report; merging remains an explicit operator/Git policy decision.
"""

from __future__ import annotations

import asyncio
import base64
import json
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
    last_diagnosis: str = ""
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
        self._seen_anomalies: set[str] = set()

    def status(self) -> dict[str, Any]:
        return asdict(self._state) | {"active_turns": self._active_turns, "seen_anomalies": len(self._seen_anomalies)}

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
                    for anomaly in anomalies[:3]:
                        if anomaly in self._seen_anomalies:
                            continue
                        self._seen_anomalies.add(anomaly)
                        self._state.last_anomaly = anomaly
                        await self._emit({"level": "warn", "message": anomaly[:400]})
                        await self._self_heal(anomaly)
                        break
                net = await diagnose_and_optionally_recover(self.host_exec)
                if not net.get("container_http", {}).get("ok"):
                    await self._emit({"level": "warn", "message": "Researcher network probe failed; safe recovery path evaluated.", "network": net})
        finally:
            self._state.active = False

    async def _scan_runtime_logs(self) -> list[str]:
        if self.host_exec is None:
            return []
        cmd = "docker logs --tail 240 jarvis-backend 2>&1 && docker logs --tail 160 jarvis-vllm-qwen 2>&1 && docker logs --tail 120 jarvis-vllm-uitars 2>&1"
        res = await self.host_exec(cmd)
        text = res.get("out", "")
        return [line.strip() for line in text.splitlines() if any(p.search(line) for p in ERROR_PATTERNS)][:10]

    def _classify(self, anomaly: str) -> dict[str, Any]:
        low = anomaly.lower()
        if "uva is not available" in low:
            return {"kind": "cuda_uva", "severity": "high", "suggestion": "Verify CUDA_VISIBLE_DEVICES=0, CUDA_DISABLE_P2P=1, NCCL_P2P_DISABLE=1 and enforce-eager profile."}
        if "outofmemory" in low or "out of memory" in low:
            return {"kind": "vram_oom", "severity": "high", "suggestion": "Reduce max_model_len/gpu_util, stop optional audio/UI-TARS, flush inactive contexts."}
        if "name or service not known" in low or "connection attempts failed" in low:
            return {"kind": "network", "severity": "medium", "suggestion": "Run container HTTP probe and host DNS probe; apply only configured recovery hooks."}
        if "mcp" in low:
            return {"kind": "mcp", "severity": "medium", "suggestion": "Check mcp_servers.json absolute paths, binaries, sqlite db path and restart MCP manager."}
        return {"kind": "traceback", "severity": "medium", "suggestion": "Inspect traceback tail, map to incidents ledger, prepare targeted patch."}

    async def _diagnose(self, anomaly: str, klass: dict[str, Any]) -> str:
        try:
            from . import llm
            messages = [
                {"role": "system", "content": "Ты — Coder/SysAdmin self-heal модуль JARVIS. Дай краткий технический диагноз и безопасный план проверки. Без автослияния в main."},
                {"role": "user", "content": f"Класс: {klass}\nАномалия:\n{anomaly}"},
            ]
            return (await llm.chat(messages, temperature=0.1, max_tokens=500, timeout=90)).strip()[:3000]
        except Exception as exc:  # noqa: BLE001
            return f"LLM diagnosis unavailable: {exc}. Suggested deterministic action: {klass.get('suggestion')}"

    async def _write_report(self, branch: str, anomaly: str, klass: dict[str, Any], diagnosis: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        if self.host_exec is None:
            return {"ok": False, "out": "no host_exec"}
        report = {"ts": time.time(), "branch": branch, "anomaly": anomaly, "classification": klass, "diagnosis": diagnosis, "validation": results}
        body = json.dumps(report, ensure_ascii=False, indent=2)
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        path = f"data/jarvis_core/self_heal/report_{int(time.time())}.json"
        py = (
            "import base64,pathlib;"
            f"p=pathlib.Path(r'{self.repo_path}')/r'{path}';"
            "p.parent.mkdir(parents=True,exist_ok=True);"
            f"p.write_bytes(base64.b64decode('{b64}'));"
            "print(p)"
        )
        wr = await self.host_exec(f'python -c "{py}"')
        if wr.get("ok"):
            await self.host_exec(f'git -C "{self.repo_path}" add "{path}" && git -C "{self.repo_path}" commit -m "JARVIS self-heal report: {klass.get("kind", "anomaly")}"')
        return wr

    async def _self_heal(self, anomaly: str) -> None:
        klass = self._classify(anomaly)
        diagnosis = await self._diagnose(anomaly, klass)
        self._state.last_diagnosis = diagnosis[:500]
        if not self.self_heal_enabled or self.host_exec is None:
            await self._emit({"level": "info", "message": "Self-heal diagnostic completed. Set JARVIS_SELF_HEAL_ENABLE=1 to allow branch/report/test cycles.", "classification": klass, "diagnosis": diagnosis})
            return
        branch = "fix/jarvis-auto-" + str(int(time.time()))
        steps = [
            f'git -C "{self.repo_path}" status --short',
            f'git -C "{self.repo_path}" checkout -B "{branch}"',
            f'python -m compileall "{self.repo_path}/backend"',
            f'cmd /c "cd /d {self.repo_path} && docker compose -f wsl/docker-compose.agents.yml --env-file wsl/.env config"',
        ]
        results: list[dict[str, Any]] = []
        for cmd in steps:
            r = await self.host_exec(cmd)
            results.append({"cmd": cmd, "ok": r.get("ok"), "out": (r.get("out") or "")[-1600:]})
            if not r.get("ok") and "status --short" not in cmd:
                break
        report = await self._write_report(branch, anomaly, klass, diagnosis, results)
        ok = all(r.get("ok") for r in results[1:]) and report.get("ok")
        self._state.last_action = f"self-heal branch {branch}: {'ok' if ok else 'failed'}"
        msg = (f"Sir, I detected {klass['kind']} and prepared branch {branch} with a diagnostic report. Validation passed. Shall I open or merge the repair?" if ok else f"Sir, I detected {klass['kind']}, but autonomous validation failed on branch {branch}.")
        await self._emit({"level": "ok" if ok else "err", "message": msg, "anomaly": anomaly, "classification": klass, "diagnosis": diagnosis, "results": results, "report": report})
