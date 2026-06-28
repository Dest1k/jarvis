# -*- coding: utf-8 -*-
"""
dockerapi.py — тонкий асинхронный клиент Docker Engine API через unix-сокет.

Зачем не CLI `docker`:
    backend-контейнеру для стрима логов (мониторная) и исполнения кода в sandbox
    раньше требовался бинарь `docker` внутри контейнера. Если его нет в образе —
    падало с `[Errno 2] No such file or directory`. Сокет `/var/run/docker.sock`
    монтируется в backend ВСЕГДА (см. docker-compose), поэтому общение напрямую
    по Engine API надёжнее и не зависит от наличия CLI.

Реализует ровно то, что нужно системе:
    • stream_logs(container)     — живой стрим логов контейнера (follow);
    • exec_run(container, cmd)    — выполнить команду в контейнере, вернуть
                                    (exit_code, combined_output).

Поток логов/exec в Docker (при Tty=false) МУЛЬТИПЛЕКСИРОВАН: кадры с 8-байтным
заголовком [stream(1), 0,0,0, size(4, big-endian)]. Здесь это демультиплексируется.
"""

from __future__ import annotations

import asyncio
import os
import struct
from typing import AsyncIterator

import httpx

DOCKER_SOCK = os.environ.get("JARVIS_DOCKER_SOCK", "/var/run/docker.sock")


def _client(timeout: float | None) -> httpx.AsyncClient:
    """Клиент httpx поверх unix-сокета Docker."""
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    return httpx.AsyncClient(transport=transport, base_url="http://docker",
                             timeout=timeout)


async def _demux(raw: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """
    Демультиплексировать docker-поток в текст.

    Толерантно к TTY-режиму (raw, без заголовков): если первые байты не похожи
    на валидный кадр — отдаём накопленное как есть.
    """
    buf = bytearray()
    async for chunk in raw:
        buf.extend(chunk)
        while True:
            if len(buf) < 8:
                break
            stype = buf[0]
            if stype in (0, 1, 2) and buf[1] == 0 and buf[2] == 0 and buf[3] == 0:
                size = struct.unpack(">I", bytes(buf[4:8]))[0]
                if len(buf) < 8 + size:
                    break
                payload = bytes(buf[8:8 + size])
                del buf[: 8 + size]
                yield payload.decode("utf-8", "replace")
            else:
                # raw/TTY — отдать всё накопленное и продолжить как сырой поток
                yield bytes(buf).decode("utf-8", "replace")
                buf.clear()
                break
    if buf:
        yield bytes(buf).decode("utf-8", "replace")


async def stream_logs(container: str, tail: int = 120) -> AsyncIterator[str]:
    """Асинхронный генератор СТРОК лога контейнера (follow), через Engine API."""
    url = f"/containers/{container}/logs?follow=1&stdout=1&stderr=1&tail={int(tail)}"
    async with _client(timeout=None) as cli:
        async with cli.stream("GET", url) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"docker API {resp.status_code}: {body[:200]}")
            line_buf = ""
            async for text in _demux(resp.aiter_raw()):
                line_buf += text
                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    yield line.rstrip("\r")
            if line_buf:
                yield line_buf


async def exec_run(container: str, cmd: list[str],
                   timeout: float = 90.0) -> tuple[int | None, str]:
    """
    Выполнить команду в контейнере (docker exec через API).

    Возвращает (exit_code, combined_output). stdin не прикрепляем — вызывающий
    код передаёт ввод через файл внутри контейнера (см. run_code).
    """
    async with _client(timeout=timeout + 30) as cli:
        create = await cli.post(
            f"/containers/{container}/exec",
            json={"AttachStdin": False, "AttachStdout": True,
                  "AttachStderr": True, "Tty": False, "Cmd": cmd},
        )
        create.raise_for_status()
        exec_id = create.json()["Id"]

        chunks: list[str] = []

        async def _collect() -> None:
            async with cli.stream(
                "POST", f"/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
            ) as resp:
                resp.raise_for_status()
                async for text in _demux(resp.aiter_raw()):
                    chunks.append(text)

        try:
            await asyncio.wait_for(_collect(), timeout=timeout + 25)
        except asyncio.TimeoutError:
            return None, "".join(chunks) + "\n[таймаут ожидания вывода exec]"

        try:
            insp = await cli.get(f"/exec/{exec_id}/json")
            code = insp.json().get("ExitCode")
        except Exception:  # noqa: BLE001
            code = None
        return code, "".join(chunks)
