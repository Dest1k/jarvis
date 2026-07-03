# -*- coding: utf-8 -*-
"""
inference.py — двухрежимный инференс-конвейер JARVIS v2.0 (Core Ultra 9 /
128 ГБ DDR5 / RTX 5090 32 ГБ, единый OpenAI-совместимый бэкенд vLLM).

Веса ПРОВЕРЕНЫ на HuggingFace (реальные NVFP4-кванты NVIDIA под Blackwell):

    РЕЖИМ 1 — «moe-turbo»: nvidia/Gemma-4-26B-A4B-NVFP4
        База google/gemma-4-26B-A4B-it. MoE: 25.2B всего / 3.8B активных
        (8 из 128 экспертов). NVFP4 ≈ 16.5 ГБ на диске. vLLM TP=1.
        Максимум токенов/с: активна лишь ~4B на токен. Агрессивное
        префикс-кэширование (RadixAttention-эквивалент --enable-prefix-caching)
        и до 90% VRAM под пул KV/префикс-блоков. UI-TARS отключается — вся
        карта отдана скорости диспетчера.

    РЕЖИМ 2 — «dense-hybrid»: nvidia/Gemma-4-31B-IT-NVFP4
        База google/gemma-4-31B-it. Плотная, 30.7B. NVFP4 ≈ 21 ГБ на диске —
        РЕЗИДЕНТНО помещается в 32 ГБ (веса 21 + KV в остатке). Гибридный
        оффлоад (--cpu-offload-gb) переносит часть параметров в 128-ГБ
        host-RAM, освобождая VRAM под больший контекст и сосуществование с
        UI-TARS/аудио; --swap-space страхует KV от OOM. Это НЕ спасение от
        неминуемого OOM (модель и так влезает), а осознанный размен VRAM на
        host-RAM ради запаса. Требует --quantization modelopt.

Модуль — ЧИСТЫЙ реестр режимов (без I/O): вычисляет наборы env-переменных
для docker-compose и распознаёт активный режим по содержимому wsl/.env.
Оркестрацию (запись .env, последовательный рестарт контейнеров) выполняет
server.py через RPC-мост — так режимы переключаются кнопкой из дашборда.
"""

from __future__ import annotations

from typing import Any, Optional

# Ключи env, которыми режим управляет (совпадают с docker-compose.agents.yml).
_MANAGED_KEYS = (
    "JARVIS_QWEN_MODEL_PATH", "JARVIS_QWEN_QUANT_ARGS", "JARVIS_QWEN_DTYPE",
    "JARVIS_QWEN_GPU_UTIL", "JARVIS_QWEN_MAX_LEN", "JARVIS_QWEN_KV_DTYPE",
    "JARVIS_QWEN_EXTRA_ARGS", "JARVIS_ENABLE_UITARS",
)

# Общие флаги Gemma 4 для vLLM: доверенный код архитектуры + встроенные парсеры
# инструментов/reasoning (Gemma 4 — reasoning-модель; без парсера reasoning
# «утекает» в контент). Наш агент использует two-phase JSON, поэтому
# auto-tool-choice НЕ включаем, чтобы не конфликтовать с собственным протоколом.
_GEMMA4_COMMON = "--trust-remote-code --reasoning-parser gemma4"

MODES: dict[str, dict[str, Any]] = {
    "moe-turbo": {
        "label": "Режим 1 · MoE-турбо: Gemma-4-26B-A4B (NVFP4), префикс-кэш 90% VRAM",
        "model_repo": "nvidia/Gemma-4-26B-A4B-NVFP4",
        "model_name": "gemma4-26b-a4b-nvfp4",
        "summary": (
            "Максимальная скорость (MoE: 25.2B всего, активно 3.8B на токен). "
            "Веса NVFP4 ~16.5 ГБ + пул KV/префикс ~11 ГБ при util 0.90. "
            "Префикс-кэширование агрессивное: системные промпты агента "
            "переиспользуются между шагами ReAct без пересчёта. "
            "UI-TARS отключён — карта отдана диспетчеру. vLLM TP=1."
        ),
        "vram": "0.90 × 32 = 28.8 ГБ (веса ~16.5 + overhead ~0.8 + KV/префикс ~11.5); "
                "аудио ~2.0 в остатке; резерв десктопа ~1.2 ГБ.",
        "env": {
            "JARVIS_QWEN_MODEL_PATH": "/models/gemma4-26b-a4b-nvfp4",
            "JARVIS_QWEN_QUANT_ARGS": "",   # NVFP4 определяется из config модели
            "JARVIS_QWEN_DTYPE": "auto",
            "JARVIS_QWEN_GPU_UTIL": "0.90",
            "JARVIS_QWEN_MAX_LEN": "32768",
            "JARVIS_QWEN_KV_DTYPE": "fp8",
            "JARVIS_QWEN_EXTRA_ARGS": f"{_GEMMA4_COMMON} --max-num-seqs 16",
            "JARVIS_ENABLE_UITARS": "0",
        },
    },
    "dense-hybrid": {
        "label": "Режим 2 · Dense-гибрид: Gemma-4-31B-IT (NVFP4) + оффлоад в 128 ГБ RAM",
        "model_repo": "nvidia/Gemma-4-31B-IT-NVFP4",
        "model_name": "gemma4-31b-it-nvfp4",
        "summary": (
            "Максимальное качество (плотная 30.7B). NVFP4 ~21 ГБ РЕЗИДЕНТНО "
            "помещается в 32 ГБ. Гибридный оффлоад (--cpu-offload-gb 8) "
            "переносит часть весов в pinned host-RAM (DMA по PCIe 5.0, prefetch "
            "перекрыт вычислением), освобождая VRAM под больший контекст и "
            "сосуществование с UI-TARS-2B; --swap-space 8 страхует KV от OOM. "
            "Требует --quantization modelopt. vLLM TP=1."
        ),
        "vram": "GPU util 0.68 × 32 = 21.8 ГБ (веса-резидент ~13 + KV ~8); "
                "~8 ГБ весов и до 8 ГБ KV-свопа в host-RAM (из 128); "
                "UI-TARS-2B ~5 + аудио ~2 в GPU-остатке.",
        "env": {
            "JARVIS_QWEN_MODEL_PATH": "/models/gemma4-31b-it-nvfp4",
            "JARVIS_QWEN_QUANT_ARGS": "--quantization modelopt",
            "JARVIS_QWEN_DTYPE": "auto",
            "JARVIS_QWEN_GPU_UTIL": "0.68",
            "JARVIS_QWEN_MAX_LEN": "16384",
            "JARVIS_QWEN_KV_DTYPE": "fp8",
            "JARVIS_QWEN_EXTRA_ARGS": f"{_GEMMA4_COMMON} --cpu-offload-gb 8 --swap-space 8",
            "JARVIS_ENABLE_UITARS": "1",
        },
    },
}


def describe() -> dict[str, Any]:
    """Каталог режимов для дашборда (без env-простыни)."""
    return {
        mid: {k: m[k] for k in ("label", "model_repo", "summary", "vram")}
        for mid, m in MODES.items()
    }


def env_updates(mode_id: str) -> Optional[dict[str, str]]:
    """Полный набор env-переменных режима (для записи в wsl/.env)."""
    mode = MODES.get(mode_id)
    if mode is None:
        return None
    updates = {k: "" for k in _MANAGED_KEYS}   # управляемые ключи всегда переопределяем
    updates.update(mode["env"])
    return updates


def detect_mode(env_text: str) -> Optional[str]:
    """Определить активный режим по содержимому wsl/.env (по пути модели)."""
    for mid, mode in MODES.items():
        path = mode["env"]["JARVIS_QWEN_MODEL_PATH"]
        if f"JARVIS_QWEN_MODEL_PATH={path}" in (env_text or ""):
            return mid
    return None
