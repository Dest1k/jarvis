# -*- coding: utf-8 -*-
"""Tests for native host tool registration and idle self-heal classification.

Run:
    python backend/tests/test_native_runtime.py
    pytest backend/tests/test_native_runtime.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
for p in (str(BACKEND), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("JARVIS_MEMORY_DIR", "/tmp/jarvis-test-memory-native")

from orchestrator import agent  # noqa: E402
from orchestrator.idle_loop import BackgroundIdleLoop  # noqa: E402


def test_native_tools_registered() -> None:
    names = set(agent._registry.names())  # type: ignore[attr-defined]
    assert {"native_host", "native_window", "native_ui"}.issubset(names), names


def test_idle_classifier_known_incidents() -> None:
    loop = BackgroundIdleLoop(host_exec=None, broadcast=None, gpu_guard=None)
    assert loop._classify("RuntimeError: UVA is not available")["kind"] == "cuda_uva"
    assert loop._classify("CUDA out of memory. Tried to allocate")["kind"] == "vram_oom"
    assert loop._classify("[Errno -2] Name or service not known")["kind"] == "network"
    assert loop._classify("MCP sqlite server not initialize")["kind"] == "mcp"


async def _main() -> int:
    test_native_tools_registered()
    test_idle_classifier_known_incidents()
    print("PASS · native runtime tests")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
