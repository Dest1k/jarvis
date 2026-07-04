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
        под пул KV/префикс-блоков (util 0.85 — потолок 90% по ТЗ, но оставляем
        ~4.8 ГБ под аудио и рабочий стол, иначе они голодают на 32 ГБ). UI-TARS
        отключается — карта отдана скорости диспетчера.

    РЕЖИМ 2 — «dense-hybrid» (СОЛО): nvidia/Gemma-4-31B-IT-NVFP4
        База google/gemma-4-31B-it. Плотная, 30.7B, мультимодальная — экран
        видит сама, отдельный UI-TARS НЕ поднимается. NVFP4 ≈ 21 ГБ на диске.
        Гибридный оффлоад (--cpu-offload-gb) переносит часть параметров в
        128-ГБ host-RAM, высвобождая VRAM под KV-кэш; --swap-space страхует
        KV от OOM. Это НЕ спасение от неминуемого OOM (модель и так влезает),
        а осознанный размен VRAM на host-RAM ради запаса. Требует
        --quantization modelopt.

    Оба режима — СОЛО: JARVIS_ENABLE_UITARS=0, зрение (see_screen/gui)
    перенаправлено на эндпоинт диспетчера. Второй vLLM не поднимается — на
    32-ГБ карте ему нет места рядом с агрессивным KV-пулом (OOM-цикл).

Модуль — ЧИСТЫЙ реестр режимов (без I/O): вычисляет наборы env-переменных
для docker-compose и распознаёт активный режим по содержимому wsl/.env.
Оркестрацию (запись .env, последовательный рестарт контейнеров) выполняет
server.py через RPC-мост — так режимы переключаются кнопкой из дашборда.
"""

from __future__ import annotations

from typing import Any, Optional

# Ключи env, которыми режим управляет (совпадают с docker-compose.agents.yml).
# ВАЖНО: vision-ключи (JARVIS_UITARS_URL и т.д.) входят в набор — иначе при
# переключении с двойного профиля в .env залипал бы адрес выключенного UI-TARS
# и «зрение» било бы в мёртвый эндпоинт.
_MANAGED_KEYS = (
    "JARVIS_QWEN_MODEL_PATH", "JARVIS_QWEN_QUANT_ARGS", "JARVIS_QWEN_DTYPE",
    "JARVIS_QWEN_GPU_UTIL", "JARVIS_QWEN_MAX_LEN", "JARVIS_QWEN_KV_DTYPE",
    "JARVIS_QWEN_MAX_NUM_SEQS", "JARVIS_QWEN_EXTRA_ARGS", "JARVIS_ENABLE_UITARS",
    "JARVIS_UITARS_URL", "JARVIS_UITARS_MODEL_NAME", "JARVIS_UITARS_MAX_LEN",
    "JARVIS_UITARS_COORD_MODE", "JARVIS_VISION_MODEL",
)

# СОЛО-редирект зрения: обе Gemma 4 мультимодальны — see_screen/gui обслуживает
# сам диспетчер, отдельный UI-TARS не поднимается (экономия ~5-6 ГБ VRAM).
def _solo_vision(max_len: str) -> dict[str, str]:
    return {
        "JARVIS_UITARS_URL": "http://vllm-qwen-coder:8001/v1",
        "JARVIS_UITARS_MODEL_NAME": "qwen-coder",
        "JARVIS_UITARS_MAX_LEN": max_len,
        "JARVIS_UITARS_COORD_MODE": "auto",
        "JARVIS_VISION_MODEL": "dispatcher",
        "JARVIS_ENABLE_UITARS": "0",
    }

# Общие флаги Gemma 4 для vLLM. ВАЖНО: НИКАКИХ --reasoning-parser/--tool-call-parser!
# vLLM валидирует значения этих флагов по списку известных парсеров, и на сборке
# без парсера 'gemma4' процесс умирает МГНОВЕННО на разборе аргументов →
# рестарт-цикл контейнера (полевой инцидент). Наш агент работает по собственному
# двухфазному JSON-протоколу и в серверных парсерах не нуждается; возможные
# <thought>-утечки reasoning в контент вычищает llm.extract_json.
#
# ВАЖНО: --max-num-seqs здесь БОЛЬШЕ НЕ ставим! Он задаётся отдельным ключом
# JARVIS_QWEN_MAX_NUM_SEQS (compose подставляет ровно один раз). Если продублировать
# его и в EXTRA_ARGS, и в базовой команде — vLLM ругается «Found duplicate keys
# --max-num-seqs» и на строгих сборках падает на разборе аргументов (движок не
# стартует). Один флаг — один канал.
_GEMMA4_COMMON = "--trust-remote-code"

MODES: dict[str, dict[str, Any]] = {
    "moe-turbo": {
        "label": "Режим 1 · MoE-турбо: Gemma-4-26B-A4B (NVFP4), префикс-кэш 90% VRAM",
        "model_repo": "nvidia/Gemma-4-26B-A4B-NVFP4",
        "model_name": "gemma4-26b-a4b-nvfp4",
        "summary": (
            "Максимальная скорость (MoE: 25.2B всего, активно 3.8B на токен). "
            "Веса NVFP4 ~16.5 ГБ + пул KV/префикс ~9 ГБ при util 0.85. "
            "Префикс-кэширование агрессивное: системные промпты агента "
            "переиспользуются между шагами ReAct без пересчёта. "
            "UI-TARS отключён — карта отдана диспетчеру. vLLM TP=1."
        ),
        # util 0.85 (а не 0.90): оставляем ~4.8 ГБ под аудио (~2) и рабочий стол
        # Windows (~2.8) — иначе на 32 ГБ карте десктоп/аудио голодают и падают.
        "vram": "0.85 × 32 = 27.2 ГБ (веса ~16.5 + overhead ~0.8 + KV/префикс ~9.9); "
                "аудио ~2.0 + резерв десктопа ~2.8 ГБ.",
        "env": {
            "JARVIS_QWEN_MODEL_PATH": "/models/gemma4-26b-a4b-nvfp4",
            "JARVIS_QWEN_QUANT_ARGS": "",   # NVFP4 определяется из config модели
            "JARVIS_QWEN_DTYPE": "auto",
            "JARVIS_QWEN_GPU_UTIL": "0.85",
            "JARVIS_QWEN_MAX_LEN": "32768",
            "JARVIS_QWEN_KV_DTYPE": "fp8",
            "JARVIS_QWEN_MAX_NUM_SEQS": "16",   # выше конкуренция запросов (MoE быстра)
            "JARVIS_QWEN_EXTRA_ARGS": _GEMMA4_COMMON,
            **_solo_vision("32768"),
        },
    },
    "dense-hybrid": {
        "label": "Режим 2 · Dense-гибрид (СОЛО): Gemma-4-31B-IT (NVFP4) + оффлоад в 128 ГБ RAM",
        "model_repo": "nvidia/Gemma-4-31B-IT-NVFP4",
        "model_name": "gemma4-31b-it-nvfp4",
        "summary": (
            "Максимальное качество (плотная 30.7B, мультимодальная — экран видит "
            "САМА, UI-TARS не поднимается). NVFP4 ~21 ГБ; гибридный оффлоад "
            "(--cpu-offload-gb 8) переносит часть весов в pinned host-RAM (DMA "
            "по PCIe 5.0, prefetch перекрыт вычислением), высвобождая VRAM под "
            "KV-кэш; --swap-space 8 страхует KV от OOM. "
            "Требует --quantization modelopt. vLLM TP=1."
        ),
        "vram": "GPU util 0.75 × 32 = 24.0 ГБ (веса-резидент ~13 + overhead ~1 + KV ~10); "
                "~8 ГБ весов и до 8 ГБ KV-свопа в host-RAM (из 128); "
                "аудио ~2 + резерв десктопа ~6 ГБ. UI-TARS НЕ поднимается.",
        "env": {
            "JARVIS_QWEN_MODEL_PATH": "/models/gemma4-31b-it-nvfp4",
            "JARVIS_QWEN_QUANT_ARGS": "--quantization modelopt",
            "JARVIS_QWEN_DTYPE": "auto",
            "JARVIS_QWEN_GPU_UTIL": "0.75",
            "JARVIS_QWEN_MAX_LEN": "16384",
            "JARVIS_QWEN_KV_DTYPE": "fp8",
            "JARVIS_QWEN_EXTRA_ARGS": f"{_GEMMA4_COMMON} --cpu-offload-gb 8 --swap-space 8",
            **_solo_vision("16384"),
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
