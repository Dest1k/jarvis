#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_layer.py — низколатентный аудио-слой JARVIS v2.0: ASR (Faster-Whisper
Large-v3) + TTS (Kokoro), с изоляцией приоритета потоков.

Проблема, которую решает v2.0:
    Тяжёлые фазы исполнения (инференс диспетчера, компиляция кода, git-разведка)
    не должны «голодать» аудио-конвейер — иначе голос заикается и рвётся. И
    веса аудио-моделей нельзя ронять в своп, конкурируя с контекстным окном
    основной LLM.

Инженерные меры:
    • ВЫДЕЛЕННЫЕ пулы потоков для ASR и TTS (не общий default-executor asyncio,
      который делит потоки со всем backend). ASR- и TTS-задачи не блокируют друг
      друга и не стоят в общей очереди.
    • ПРИОРИТЕТНАЯ ИЗОЛЯЦИЯ: рабочие потоки аудио поднимают свой планировочный
      приоритет (nice/SCHED), чтобы под нагрузкой CPU их не вытесняли фоновые
      задачи. Мягкая деградация, если прав недостаточно.
    • ЗАКРЕПЛЕНИЕ ВЕСОВ В ПАМЯТИ (mlock через MADV/ mlockall best-effort), чтобы
      веса ASR/TTS не уходили в своп и не сталкивались с KV-кэшем основной LLM.
    • ПОТОКОВЫЙ TTS ЧЕРЕЗ ОЧЕРЕДЬ: чанки аудио отдаются клиенту ПО МЕРЕ синтеза
      (producer в пуле TTS → asyncio.Queue → сеть), а не «сгенерировать всё,
      потом отдать». Реальная низкая задержка первого звука.
    • ПРОГРЕВ В ФОНЕ: модели грузятся при старте в своих пулах, /health отвечает
      сразу — оркестратор не ждёт прогрева.

Предоставляет:
    GET  /health        — готовность (+ статус прогрева моделей).
    WS   /ws/asr        — потоковое распознавание речи с VAD.
    POST /tts/stream    — низколатентный потоковый синтез (HEVC-эра: аудио).

VRAM: Faster-Whisper Large-v3 (int8_float16) ~1.5 ГБ + Kokoro ~0.4 ГБ ≈ 2 ГБ.
Зависимости: faster-whisper, kokoro (или kokoro-onnx), webrtcvad, numpy, fastapi.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import struct
from concurrent.futures import ThreadPoolExecutor
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
# Насколько поднять приоритет аудио-потоков (отрицательный nice = выше приоритет).
AUDIO_NICE = int(os.environ.get("JARVIS_AUDIO_NICE", "-10"))
# Закреплять ли веса аудио в RAM (mlockall). По умолчанию включено.
PIN_AUDIO_MEMORY = os.environ.get("JARVIS_AUDIO_MLOCK", "1") == "1"


# --------------------------------------------------------------------------- #
# Приоритетная изоляция потоков и закрепление памяти
# --------------------------------------------------------------------------- #
def _elevate_thread_priority(tag: str) -> None:
    """
    Поднять планировочный приоритет ТЕКУЩЕГО потока (best-effort).

    На Linux пробуем и per-thread nice (setpriority с tid), и — при наличии
    прав — SCHED_RR realtime-класс. Любой отказ (нет прав) — мягкая деградация:
    аудио продолжит работать на обычном приоритете, просто менее устойчиво под
    экстремальной нагрузкой.
    """
    try:
        os.nice(0)  # тронуть, чтобы гарантировать наличие модуля/поддержки
    except Exception:  # noqa: BLE001
        pass
    if not hasattr(os, "setpriority"):
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        tid = libc.syscall(186)  # SYS_gettid на x86_64
        # PRIO_PROCESS=0; для потока используем его tid как «pid».
        libc.setpriority(0, tid, AUDIO_NICE)
        log.info("Аудио-поток '%s' повысил приоритет (nice=%d).", tag, AUDIO_NICE)
    except Exception as exc:  # noqa: BLE001
        log.info("Аудио-поток '%s': приоритет не поднят (%s) — работаю штатно.",
                 tag, exc)


def _pin_process_memory() -> None:
    """
    Закрепить страницы процесса в RAM (mlockall MCL_CURRENT|MCL_FUTURE),
    чтобы веса ASR/TTS не вытеснялись в своп под давлением KV-кэша основной LLM.
    Best-effort: без CAP_IPC_LOCK/лимитов просто пропускаем.
    """
    if not PIN_AUDIO_MEMORY:
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        MCL_CURRENT, MCL_FUTURE = 1, 2
        if libc.mlockall(MCL_CURRENT | MCL_FUTURE) == 0:
            log.info("Веса аудио закреплены в RAM (mlockall) — своп им не грозит.")
        else:
            err = ctypes.get_errno()
            log.info("mlockall недоступен (errno=%d) — продолжаю без пиннинга.", err)
    except Exception as exc:  # noqa: BLE001
        log.info("Пиннинг памяти пропущен (%s).", exc)


def _pool(tag: str) -> ThreadPoolExecutor:
    """Однопоточный пул с повышенным приоритетом и инициализатором потока."""
    return ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=f"jarvis-audio-{tag}",
        initializer=_elevate_thread_priority, initargs=(tag,))


# ВЫДЕЛЕННЫЕ пулы: ASR и TTS не делят потоки ни между собой, ни с backend.
_asr_pool = _pool("asr")
_tts_pool = _pool("tts")

app = FastAPI(title="JARVIS v2.0 Audio Layer", version="2.0.0")

# Ленивая инициализация моделей (грузятся в своих пулах фоновым прогревом).
_whisper = None
_kokoro = None
_warm = {"asr": False, "tts": False}


def _load_whisper():
    """Загрузка Faster-Whisper на GPU (в ASR-пуле)."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        log.info("Загрузка Faster-Whisper %s (%s) на GPU…", WHISPER_MODEL, WHISPER_COMPUTE)
        _pin_process_memory()
        _whisper = WhisperModel(WHISPER_MODEL, device="cuda", compute_type=WHISPER_COMPUTE)
        _warm["asr"] = True
    return _whisper


def _load_kokoro():
    """Загрузка Kokoro TTS (в TTS-пуле)."""
    global _kokoro
    if _kokoro is None:
        try:
            from kokoro import KPipeline
            log.info("Загрузка Kokoro TTS (голос %s)…", KOKORO_VOICE)
            _kokoro = KPipeline(lang_code="a")  # 'a' — авто/английский; для RU см. доку
        except Exception as exc:  # noqa: BLE001
            log.warning("Kokoro недоступен (%s). TTS вернёт тишину-заглушку.", exc)
            _kokoro = False
    _warm["tts"] = True
    return _kokoro


@app.on_event("startup")
async def _warmup() -> None:
    """Прогреть модели в фоне: /health отвечает сразу, оркестратор не ждёт."""
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_asr_pool, _load_whisper)
    loop.run_in_executor(_tts_pool, _load_kokoro)
    log.info("Фоновый прогрев ASR/TTS запущен в изолированных пулах.")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "warm": dict(_warm)})


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
    """Распознать PCM16-сегмент через Faster-Whisper в ВЫДЕЛЕННОМ ASR-пуле."""
    def _blocking() -> str:
        model = _load_whisper()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = model.transcribe(audio, language=None, beam_size=1, vad_filter=False)
        return " ".join(seg.text for seg in segments).strip()

    return await asyncio.get_running_loop().run_in_executor(_asr_pool, _blocking)


# --------------------------------------------------------------------------- #
# TTS: низколатентный потоковый синтез Kokoro через очередь
# --------------------------------------------------------------------------- #
@app.post("/tts/stream")
async def tts_stream(payload: dict[str, Any]) -> StreamingResponse:
    """
    Стримить аудио-чанки синтеза ПО МЕРЕ генерации (low-latency).

    Producer работает в ВЫДЕЛЕННОМ TTS-пуле и кладёт готовые PCM-чанки в
    asyncio.Queue; сетевой consumer отдаёт их клиенту немедленно. Так первый
    звук уходит сразу после синтеза первой фразы, не дожидаясь конца текста.
    """
    text = str(payload.get("text", "")).strip()

    async def _generate():
        yield _wav_header(SAMPLE_RATE)
        if not text:
            return
        pipeline = _load_kokoro()
        if pipeline is False:
            # Заглушка-тишина (1 кадр), если Kokoro недоступен.
            yield struct.pack("<" + "h" * SAMPLE_RATE, *([0] * SAMPLE_RATE))
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        _DONE = object()

        def _produce() -> None:
            # Kokoro отдаёт аудио по фразам/графемам — публикуем каждую сразу.
            try:
                for _, _, audio in pipeline(text, voice=KOKORO_VOICE):
                    arr = np.asarray(audio)
                    pcm16 = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
                    loop.call_soon_threadsafe(queue.put_nowait, pcm16.tobytes())
            except Exception as exc:  # noqa: BLE001
                log.warning("Сбой синтеза TTS: %s", exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        # Producer — в TTS-пуле (приоритетно, не блокирует ASR и backend).
        _tts_pool.submit(_produce)
        while True:
            chunk = await queue.get()
            if chunk is _DONE:
                break
            if chunk:
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


@app.on_event("shutdown")
async def _shutdown() -> None:
    _asr_pool.shutdown(wait=False, cancel_futures=True)
    _tts_pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("audio_layer:app", host="0.0.0.0", port=8003, log_level="info")
