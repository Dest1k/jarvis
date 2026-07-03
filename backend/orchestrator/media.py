# -*- coding: utf-8 -*-
"""
media.py — мультимедиа-подсистема JARVIS v2.0 (HEVC-first).

Инженерный стандарт v2.0: любая функция записи/перекодирования видео по
умолчанию использует эффективный аппаратно-ускоренный кодек HEVC (H.265).
Порядок предпочтения энкодеров:

    1. hevc_nvenc  — NVENC на RTX 5090 (нулевая нагрузка на CUDA-ядра,
                     не конкурирует с инференсом LLM за SM);
    2. hevc_qsv    — Quick Sync на iGPU Core Ultra 9 (полностью разгружает
                     дискретную карту);
    3. hevc_vaapi  — универсальный VA-API путь;
    4. libx265     — программный фолбэк (медленно, но всегда работает).

Выбор кэшируется на процесс: определяется однократным опросом
`ffmpeg -encoders` + пробным кодированием одного кадра (наличие энкодера в
сборке ffmpeg ещё не значит, что железо/драйвер доступны из контейнера).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from .incidents import CORE_DIR

log = logging.getLogger("jarvis.media")

MEDIA_DIR = CORE_DIR / "media"

# Порядок предпочтения HEVC-энкодеров и их тюнинг под низкую задержку.
_HEVC_PREFERENCE: tuple[tuple[str, list[str]], ...] = (
    ("hevc_nvenc", ["-preset", "p5", "-tune", "ll", "-rc", "vbr", "-cq", "28"]),
    ("hevc_qsv",   ["-preset", "fast", "-global_quality", "28"]),
    ("hevc_vaapi", ["-qp", "28"]),
    ("libx265",    ["-preset", "fast", "-crf", "26"]),
)

_detected: Optional[tuple[str, list[str]]] = None
_detect_lock = asyncio.Lock()


async def _ffmpeg(*args: str, timeout: float = 30.0) -> tuple[int, str]:
    """Запустить ffmpeg, вернуть (rc, stderr+stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "ffmpeg не установлен"
    except asyncio.TimeoutError:
        return 124, "ffmpeg: таймаут"


async def detect_hevc_encoder() -> tuple[str, list[str]]:
    """
    Определить лучший ДОСТУПНЫЙ HEVC-энкодер (кэшируется на процесс).

    Сначала список `-encoders`, затем пробное кодирование одного тестового
    кадра выбранным энкодером — так отсеиваются энкодеры, собранные в ffmpeg,
    но не имеющие рабочего железа/драйвера в контейнере.
    """
    global _detected
    if _detected is not None:
        return _detected
    async with _detect_lock:
        if _detected is not None:
            return _detected
        rc, listing = await _ffmpeg("-encoders", timeout=15)
        available = listing if rc == 0 else ""
        for name, tune in _HEVC_PREFERENCE:
            if name not in available:
                continue
            rc, _out = await _ffmpeg(
                "-f", "lavfi", "-i", "color=black:s=320x240:d=0.1",
                "-frames:v", "1", "-c:v", name, *tune, "-f", "null", "-",
                timeout=25)
            if rc == 0:
                log.info("HEVC-энкодер выбран: %s", name)
                _detected = (name, tune)
                return _detected
        log.warning("Ни один HEVC-энкодер не прошёл пробу — фолбэк libx265.")
        _detected = _HEVC_PREFERENCE[-1]
        return _detected


async def record_display(duration: int = 10, *, display: Optional[str] = None,
                         size: str = "1280x720", fps: int = 30,
                         out_path: Optional[str] = None) -> dict[str, Any]:
    """
    Записать виртуальный десктоп (Xvfb) в HEVC-MP4.

    Возвращает {ok, content, path?}: content — человекочитаемый итог для
    наблюдения агента (по-русски), path — файл записи в .jarvis_core/media.
    """
    duration = max(1, min(int(duration), 600))
    display = display or os.environ.get("JARVIS_XDISPLAY", ":99")
    encoder, tune = await detect_hevc_encoder()

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(out_path) if out_path else (
        MEDIA_DIR / f"desktop_{int(time.time())}.mp4")

    args = [
        "-loglevel", "error", "-y",
        "-f", "x11grab", "-video_size", size, "-framerate", str(fps),
        "-t", str(duration), "-i", display,
        "-c:v", encoder, *tune,
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out),
    ]
    rc, log_out = await _ffmpeg(*args, timeout=duration + 30)
    if rc != 0:
        return {"ok": False,
                "content": (f"Запись экрана не удалась (ffmpeg rc={rc}, "
                            f"энкодер {encoder}): {log_out[-400:]}")}
    try:
        size_mb = out.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    return {"ok": True,
            "content": (f"Запись готова, сэр: {out} — {duration} с, {size} @ "
                        f"{fps} fps, кодек HEVC ({encoder}), {size_mb:.1f} МБ."),
            "path": str(out), "encoder": encoder}
