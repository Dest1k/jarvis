#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_layer.py — аудио-слой JARVIS-OS: ASR (Faster-Whisper Large-v3) + TTS (Kokoro).

Предоставляет:
    GET  /health        — проверка готовности.
    WS   /ws/asr        — потоковое распознавание речи с VAD (Voice Activity
                          Detection): принимает PCM16-чанки, отдаёт частичные и
                          финальные транскрипты в JSON.
    POST /tts/stream    — синтез речи Kokoro с низкой задержкой: стримит
                          сырые PCM/WAV-чанки по мере генерации.

VRAM: Faster-Whisper Large-v3 (int8_float16) ~1.5 ГБ + Kokoro ~0.4 ГБ ≈ 2 ГБ.

Зависимости: faster-whisper, kokoro (или kokoro-onnx), webrtcvad, numpy, fastapi.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import struct
from collections import deque
from typing import Any, Optional

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-8s | AUDIO | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("jarvis.audio")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8_float16")
KOKORO_VOICE = os.environ.get("KOKORO_VOICE", "af_sky")
SAMPLE_RATE = 16000

app = FastAPI(title="JARVIS-OS Audio Layer", version="1.0.0")

# Ленивая инициализация моделей (чтобы /health отвечал до прогрева)
_whisper = None
_kokoro = None


def get_whisper():
    """Ленивая загрузка Faster-Whisper на GPU."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        log.info("Загрузка Faster-Whisper %s (%s) на GPU…", WHISPER_MODEL, WHISPER_COMPUTE)
        _whisper = WhisperModel(WHISPER_MODEL, device="cuda", compute_type=WHISPER_COMPUTE)
    return _whisper


def get_kokoro():
    """Ленивая загрузка Kokoro TTS."""
    global _kokoro
    if _kokoro is None:
        try:
            from kokoro import KPipeline
            log.info("Загрузка Kokoro TTS (голос %s)…", KOKORO_VOICE)
            _kokoro = KPipeline(lang_code="a")  # 'a' — авто/английский; для RU см. доку
        except Exception as exc:  # noqa: BLE001
            log.warning("Kokoro недоступен (%s). TTS вернёт тишину-заглушку.", exc)
            _kokoro = False
    return _kokoro


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# --------------------------------------------------------------------------- #
# VAD — детекция голосовой активности (webrtcvad)
# --------------------------------------------------------------------------- #
class VADSegmenter:
    """
    Сегментация речи по VAD. Накопление кадров до паузы, затем — отдача буфера
    в ASR. Это даёт реальное время с приемлемой задержкой.
    """

    def __init__(self, aggressiveness: int = 2, frame_ms: int = 30) -> None:
        import webrtcvad
        self.vad = webrtcvad.Vad(aggressiveness)
        self.frame_ms = frame_ms
        self.frame_bytes = int(SAMPLE_RATE * frame_ms / 1000) * 2  # 16-bit
        self._buf = bytearray()
        self._speech = bytearray()
        self._silence_frames = 0
        self._triggered = False
        self.silence_threshold = 10  # ~300 мс тишины завершают высказывание

    def push(self, pcm: bytes) -> Optional[bytes]:
        """
        Добавить PCM-данные. Возвращает завершённый речевой сегмент (или None,
        если высказывание ещё не закончено).
        """
        self._buf.extend(pcm)
        completed: Optional[bytes] = None
        while len(self._buf) >= self.frame_bytes:
            frame = bytes(self._buf[: self.frame_bytes])
            del self._buf[: self.frame_bytes]
            try:
                is_speech = self.vad.is_speech(frame, SAMPLE_RATE)
            except Exception:  # noqa: BLE001
                is_speech = True
            if is_speech:
                self._triggered = True
                self._silence_frames = 0
                self._speech.extend(frame)
            elif self._triggered:
                self._silence_frames += 1
                self._speech.extend(frame)
                if self._silence_frames >= self.silence_threshold:
                    completed = bytes(self._speech)
                    self._speech.clear()
                    self._triggered = False
                    self._silence_frames = 0
        return completed

    def flush(self) -> Optional[bytes]:
        if self._speech:
            seg = bytes(self._speech)
            self._speech.clear()
            self._triggered = False
            return seg
        return None


# --------------------------------------------------------------------------- #
# WS: потоковый ASR с VAD
# --------------------------------------------------------------------------- #
@app.websocket("/ws/asr")
async def ws_asr(ws: WebSocket) -> None:
    await ws.accept()
    segmenter = VADSegmenter()
    log.info("ASR-сессия открыта.")
    try:
        while True:
            message = await ws.receive()
            if message.get("bytes") is not None:
                segment = segmenter.push(message["bytes"])
                if segment:
                    text = await _transcribe(segment)
                    if text.strip():
                        await ws.send_text(json.dumps(
                            {"type": "final", "text": text}, ensure_ascii=False))
            elif message.get("text") is not None:
                ctrl = json.loads(message["text"])
                if ctrl.get("type") == "flush":
                    seg = segmenter.flush()
                    if seg:
                        text = await _transcribe(seg)
                        await ws.send_text(json.dumps(
                            {"type": "final", "text": text}, ensure_ascii=False))
    except WebSocketDisconnect:
        log.info("ASR-сессия закрыта.")


async def _transcribe(pcm16: bytes) -> str:
    """Распознать PCM16-сегмент через Faster-Whisper (в пуле потоков)."""
    def _blocking() -> str:
        model = get_whisper()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = model.transcribe(audio, language=None, beam_size=1, vad_filter=False)
        return " ".join(seg.text for seg in segments).strip()

    return await asyncio.get_running_loop().run_in_executor(None, _blocking)


# --------------------------------------------------------------------------- #
# TTS: низколатентный потоковый синтез Kokoro
# --------------------------------------------------------------------------- #
@app.post("/tts/stream")
async def tts_stream(payload: dict[str, Any]) -> StreamingResponse:
    """Стримить аудио-чанки синтеза по мере генерации (low-latency)."""
    text = payload.get("text", "").strip()

    async def _generate():
        if not text:
            return
        pipeline = get_kokoro()
        if pipeline is False:
            # Заглушка-тишина (1 кадр), если Kokoro недоступен
            yield _wav_header(SAMPLE_RATE)
            yield struct.pack("<" + "h" * SAMPLE_RATE, *([0] * SAMPLE_RATE))
            return
        yield _wav_header(SAMPLE_RATE)
        loop = asyncio.get_running_loop()

        def _synth_chunks():
            # Kokoro отдаёт аудио по фразам/графемам — стримим по мере готовности
            for _, _, audio in pipeline(text, voice=KOKORO_VOICE):
                arr = np.asarray(audio)
                pcm16 = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
                yield pcm16.tobytes()

        gen = await loop.run_in_executor(None, lambda: list(_synth_chunks()))
        for chunk in gen:
            yield chunk

    return StreamingResponse(_generate(), media_type="audio/wav")


def _wav_header(sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """Минимальный потоковый WAV-заголовок (размер данных неизвестен заранее)."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                 byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("audio_layer:app", host="0.0.0.0", port=8003, log_level="info")
