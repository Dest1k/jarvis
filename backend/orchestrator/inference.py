# -*- coding: utf-8 -*-
"""inference.py — Gemma 4 runtime profiles for JARVIS OS."""

from __future__ import annotations

from typing import Any, Optional

_MANAGED_KEYS = (
    "JARVIS_QWEN_MODEL_NAME", "JARVIS_QWEN_MODEL_PATH", "JARVIS_QWEN_QUANT_ARGS",
    "JARVIS_QWEN_DTYPE", "JARVIS_QWEN_GPU_UTIL", "JARVIS_QWEN_MAX_LEN",
    "JARVIS_QWEN_KV_DTYPE", "JARVIS_QWEN_MAX_NUM_SEQS", "JARVIS_QWEN_ENFORCE_EAGER",
    "JARVIS_QWEN_EXTRA_ARGS", "JARVIS_ENABLE_UITARS", "JARVIS_UITARS_URL",
    "JARVIS_UITARS_MODEL_NAME", "JARVIS_UITARS_MAX_LEN", "JARVIS_UITARS_COORD_MODE",
    "JARVIS_VISION_MODEL",
)


def _solo_vision(max_len: str) -> dict[str, str]:
    return {
        "JARVIS_UITARS_URL": "http://vllm-qwen-coder:8001/v1",
        "JARVIS_UITARS_MODEL_NAME": "dispatcher",
        "JARVIS_UITARS_MAX_LEN": max_len,
        "JARVIS_UITARS_COORD_MODE": "auto",
        "JARVIS_VISION_MODEL": "dispatcher",
        "JARVIS_ENABLE_UITARS": "0",
    }


_GEMMA4_PATH = "/models/gemma4-26b-a4b-nvfp4"
_GEMMA4_REPO = "nvidia/Gemma-4-26B-A4B-NVFP4"

MODES: dict[str, dict[str, Any]] = {
    "gemma4-mono": {
        "label": "Gemma 4 Mono",
        "model_repo": _GEMMA4_REPO,
        "model_name": "gemma4-26b-a4b-nvfp4",
        "summary": "Stable eager dispatcher.",
        "vram": "util 0.82; max len 32k; max seqs 16.",
        "env": {
            "JARVIS_QWEN_MODEL_NAME": "dispatcher",
            "JARVIS_QWEN_MODEL_PATH": _GEMMA4_PATH,
            "JARVIS_QWEN_QUANT_ARGS": "",
            "JARVIS_QWEN_DTYPE": "auto",
            "JARVIS_QWEN_GPU_UTIL": "0.82",
            "JARVIS_QWEN_MAX_LEN": "32768",
            "JARVIS_QWEN_KV_DTYPE": "fp8",
            "JARVIS_QWEN_MAX_NUM_SEQS": "16",
            "JARVIS_QWEN_ENFORCE_EAGER": "--enforce-eager",
            "JARVIS_QWEN_EXTRA_ARGS": "",
            **_solo_vision("32768"),
        },
    },
    "gemma4-turbo": {
        "label": "Gemma 4 Turbo",
        "model_repo": _GEMMA4_REPO,
        "model_name": "gemma4-26b-a4b-nvfp4",
        "summary": "Graph dispatcher after mono is healthy.",
        "vram": "util 0.80; max len 32k; max seqs 16.",
        "env": {
            "JARVIS_QWEN_MODEL_NAME": "dispatcher",
            "JARVIS_QWEN_MODEL_PATH": _GEMMA4_PATH,
            "JARVIS_QWEN_QUANT_ARGS": "",
            "JARVIS_QWEN_DTYPE": "auto",
            "JARVIS_QWEN_GPU_UTIL": "0.80",
            "JARVIS_QWEN_MAX_LEN": "32768",
            "JARVIS_QWEN_KV_DTYPE": "fp8",
            "JARVIS_QWEN_MAX_NUM_SEQS": "16",
            "JARVIS_QWEN_ENFORCE_EAGER": "",
            "JARVIS_QWEN_EXTRA_ARGS": "",
            **_solo_vision("32768"),
        },
    },
}


def describe() -> dict[str, Any]:
    return {mid: {k: m[k] for k in ("label", "model_repo", "summary", "vram")} for mid, m in MODES.items()}


def env_updates(mode_id: str) -> Optional[dict[str, str]]:
    mode = MODES.get(mode_id)
    if mode is None:
        return None
    updates = {k: "" for k in _MANAGED_KEYS}
    updates.update(mode["env"])
    return updates


def detect_mode(env_text: str) -> Optional[str]:
    for mid, mode in MODES.items():
        path = mode["env"]["JARVIS_QWEN_MODEL_PATH"]
        maxseqs = mode["env"].get("JARVIS_QWEN_MAX_NUM_SEQS", "")
        eager = mode["env"].get("JARVIS_QWEN_ENFORCE_EAGER", "")
        text = env_text or ""
        if f"JARVIS_QWEN_MODEL_PATH={path}" in text and f"JARVIS_QWEN_MAX_NUM_SEQS={maxseqs}" in text:
            if eager and f"JARVIS_QWEN_ENFORCE_EAGER={eager}" in text:
                return mid
            if not eager and "JARVIS_QWEN_ENFORCE_EAGER=--enforce-eager" not in text:
                return mid
    return None
