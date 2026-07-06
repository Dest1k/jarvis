# -*- coding: utf-8 -*-
"""Safe GPU thermal/VRAM guard for JARVIS runtime.

The guard samples NVML when available, falls back to host nvidia-smi through the
RPC bridge, and exposes a throttle level for background/idle workloads. It does
not change driver, firmware, or global power settings; remediation is limited to
JARVIS-internal throttling and optional service cycling performed by the normal
operator/HITL path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("jarvis.gpu_guard")


@dataclass
class GpuSnapshot:
    ok: bool
    ts: float
    source: str
    name: str = ""
    util_pct: Optional[int] = None
    mem_used_mb: Optional[int] = None
    mem_total_mb: Optional[int] = None
    mem_pct: Optional[float] = None
    temp_gpu_c: Optional[int] = None
    throttle_level: int = 0
    reason: str = ""


class GpuGuard:
    """Always-on guard used by idle/background loops."""

    def __init__(self, host_exec: Callable[[str], Awaitable[dict[str, Any]]] | None = None) -> None:
        self.host_exec = host_exec
        self.warn_mem_pct = float(os.environ.get("JARVIS_GPU_WARN_MEM_PCT", "88"))
        self.crit_mem_pct = float(os.environ.get("JARVIS_GPU_CRIT_MEM_PCT", "94"))
        self.warn_temp_c = int(os.environ.get("JARVIS_GPU_WARN_TEMP_C", "78"))
        self.crit_temp_c = int(os.environ.get("JARVIS_GPU_CRIT_TEMP_C", "84"))
        self.sample_sec = max(2.0, float(os.environ.get("JARVIS_GPU_GUARD_SAMPLE_SEC", "8")))
        self._latest = GpuSnapshot(ok=False, ts=time.time(), source="init", reason="not sampled")
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._throttle_level = 0
        self._pynvml = None
        self._nvml_handle = None

    def status(self) -> dict[str, Any]:
        data = asdict(self._latest)
        data["thresholds"] = {
            "warn_mem_pct": self.warn_mem_pct,
            "crit_mem_pct": self.crit_mem_pct,
            "warn_temp_c": self.warn_temp_c,
            "crit_temp_c": self.crit_temp_c,
        }
        data["throttle_level"] = self._throttle_level
        data["throttle_delay_sec"] = self.throttle_delay()
        return data

    def throttle_delay(self) -> float:
        return (0.0, 0.15, 0.5, 1.2)[min(self._throttle_level, 3)]

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="gpu-guard")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snap = await self.sample()
                self._latest = snap
                self._throttle_level = self._derive_throttle(snap)
            except Exception as exc:  # noqa: BLE001
                self._latest = GpuSnapshot(ok=False, ts=time.time(), source="guard", reason=str(exc))
            await asyncio.sleep(self.sample_sec)

    async def sample(self) -> GpuSnapshot:
        snap = self._sample_nvml()
        if snap.ok:
            return snap
        if self.host_exec is not None:
            return await self._sample_host_nvidia_smi()
        return snap

    def _sample_nvml(self) -> GpuSnapshot:
        try:
            if self._pynvml is None:
                import pynvml  # type: ignore
                self._pynvml = pynvml
                pynvml.nvmlInit()
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            pynvml = self._pynvml
            handle = self._nvml_handle
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            total = int(mem.total / 1024 / 1024)
            used = int(mem.used / 1024 / 1024)
            pct = round(used / total * 100, 1) if total else None
            return GpuSnapshot(True, time.time(), "pynvml", str(name), int(util.gpu), used, total, pct, int(temp))
        except Exception as exc:  # noqa: BLE001
            return GpuSnapshot(False, time.time(), "pynvml", reason=str(exc))

    async def _sample_host_nvidia_smi(self) -> GpuSnapshot:
        query = (
            "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
            "--format=csv,noheader,nounits"
        )
        res = await self.host_exec(query) if self.host_exec is not None else {"ok": False, "out": "no host exec"}
        out = (res.get("out") or "").strip().splitlines()
        if not res.get("ok") or not out:
            return GpuSnapshot(False, time.time(), "nvidia-smi", reason=(res.get("out") or "no output")[:200])
        parts = [p.strip() for p in out[0].split(",")]
        try:
            name = parts[0]
            util = int(float(parts[1]))
            used = int(float(parts[2]))
            total = int(float(parts[3]))
            temp = int(float(parts[4]))
            return GpuSnapshot(True, time.time(), "nvidia-smi", name, util, used, total, round(used / total * 100, 1), temp)
        except Exception as exc:  # noqa: BLE001
            return GpuSnapshot(False, time.time(), "nvidia-smi", reason=f"parse failed: {exc}; raw={out[0][:200]}")

    def _derive_throttle(self, snap: GpuSnapshot) -> int:
        if not snap.ok:
            return 0
        level = 0
        reasons: list[str] = []
        if snap.mem_pct is not None:
            if snap.mem_pct >= self.crit_mem_pct:
                level = max(level, 3)
                reasons.append("critical VRAM pressure")
            elif snap.mem_pct >= self.warn_mem_pct:
                level = max(level, 2)
                reasons.append("high VRAM pressure")
        if snap.temp_gpu_c is not None:
            if snap.temp_gpu_c >= self.crit_temp_c:
                level = max(level, 3)
                reasons.append("critical thermal pressure")
            elif snap.temp_gpu_c >= self.warn_temp_c:
                level = max(level, 1)
                reasons.append("thermal warning")
        snap.throttle_level = level
        snap.reason = ", ".join(reasons)
        return level

    async def remediation_plan(self) -> dict[str, Any]:
        actions: list[dict[str, str]] = []
        if self._throttle_level >= 1:
            actions.append({"kind": "throttle", "detail": f"insert background delay {self.throttle_delay()}s"})
        if self._throttle_level >= 2:
            actions.append({"kind": "context_prune", "detail": "flush inactive session summaries"})
        if self._throttle_level >= 3:
            actions.append({"kind": "cycle_optional", "detail": "pause optional JARVIS pipelines until pressure drops"})
        return {"snapshot": asdict(self._latest), "actions": actions}
