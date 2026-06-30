#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — FastAPI-ядро (core controller) системы JARVIS-OS.

Работает внутри WSL2/Docker. Отвечает за высокопроизводительную маршрутизацию
WebSocket-потоков между браузерным дашбордом, vLLM-инстансами, аудио-слоем,
виртуальным десктопом и RPC-мостом хоста.

WebSocket-маршруты:
    /ws/deploy      — стрим логов всех контейнеров (мониторная) в реальном времени.
    /ws/chat        — универсальный чат с агентом: поток событий (мысли, вызовы
                      инструментов, токены ответа) + команды управления памятью.
    /ws/audio       — двунаправленный аудио-канал: входящие байты (VAD→ASR),
                      исходящие TTS-чанки (low-latency).
    /ws/desktop     — кадры виртуального десктопа (framebuffer) → Canvas.
    /ws/hitl        — мост HITL-уведомлений и решений оператора ↔ RPC-bridge.

REST:
    GET  /health            — проверка готовности.
    GET  /status            — сводный статус подсистем.
    POST /task              — постановка задачи в агент-оркестратор.
    GET  /api/agent/memory  — состояние памяти агента.
    POST /api/agent/memory  — управление памятью (reset/flush/clear/save).
    /api/control/*          — Пульт управления (сервисы, GPU, модели, конфиг).

Запуск:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


# --------------------------------------------------------------------------- #
# Журналирование
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | CORE | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis.core")


# --------------------------------------------------------------------------- #
# Конфигурация подключений (адреса сервисов внутри docker-сети)
# --------------------------------------------------------------------------- #
QWEN_URL = os.environ.get("JARVIS_QWEN_URL", "http://vllm-qwen-coder:8001/v1")
UITARS_URL = os.environ.get("JARVIS_UITARS_URL", "http://vllm-ui-tars:8002/v1")
AUDIO_URL = os.environ.get("JARVIS_AUDIO_URL", "http://audio-layer:8003")
RPC_BRIDGE_URL = os.environ.get("JARVIS_RPC_BRIDGE_URL", "ws://host.docker.internal:8765")
DESKTOP_VNC_HOST = os.environ.get("JARVIS_VNC_HOST", "127.0.0.1")
DESKTOP_VNC_PORT = int(os.environ.get("JARVIS_VNC_PORT", "5901"))

# Токен RPC-моста монтируется из ~/.jarvis/bridge.token
RPC_TOKEN_PATH = "/root/.jarvis/bridge.token"


# --------------------------------------------------------------------------- #
# Менеджер WebSocket-соединений с поддержкой широковещания по каналам
# --------------------------------------------------------------------------- #
class ConnectionManager:
    """Управление активными соединениями по логическим каналам."""

    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, channel: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._channels.setdefault(channel, set()).add(ws)
        log.info("WS подключён к каналу '%s' (всего: %d)", channel,
                 len(self._channels.get(channel, ())))

    async def disconnect(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            self._channels.get(channel, set()).discard(ws)

    async def broadcast(self, channel: str, message: dict[str, Any]) -> None:
        """Разослать JSON всем подписчикам канала."""
        data = json.dumps(message, ensure_ascii=False)
        dead = []
        for ws in list(self._channels.get(channel, ())):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            await self.disconnect(channel, ws)

    async def broadcast_bytes(self, channel: str, payload: bytes) -> None:
        """Разослать бинарные данные (аудио/кадры) подписчикам канала."""
        dead = []
        for ws in list(self._channels.get(channel, ())):
            try:
                await ws.send_bytes(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            await self.disconnect(channel, ws)


manager = ConnectionManager()


# --------------------------------------------------------------------------- #
# Клиент RPC-моста хоста (исходящее WS-подключение из WSL → Windows)
# --------------------------------------------------------------------------- #
class HostBridgeClient:
    """Поддерживает постоянное защищённое соединение с windows_rpc_bridge.py."""

    def __init__(self) -> None:
        self._ws: Optional[Any] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()
        self._token = self._read_token()

    @staticmethod
    def _read_token() -> str:
        try:
            with open(RPC_TOKEN_PATH, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            log.warning("Токен RPC-моста не найден (%s). RPC будет недоступен до его появления.",
                        RPC_TOKEN_PATH)
            return ""

    async def run_forever(self) -> None:
        """Цикл переподключения к RPC-мосту."""
        backoff = 2
        while True:
            try:
                self._token = self._token or self._read_token()
                # max_size большой: скриншоты 4K в base64 легко перешагивают 16 МБ
                # и рвут соединение (1009 message too big) — это и есть «мост
                # отваливается». ping_timeout щедрый: одиночная медленная команда
                # не должна ронять keep-alive.
                async with websockets.connect(
                    RPC_BRIDGE_URL, max_size=64 * 1024 * 1024,
                    ping_interval=20, ping_timeout=60, close_timeout=10,
                ) as ws:
                    await ws.send(json.dumps({"type": "auth", "token": self._token,
                                              "role": "orchestrator"}))
                    auth = json.loads(await ws.recv())
                    if not auth.get("ok"):
                        log.error("RPC-мост отклонил авторизацию: %s", auth.get("error"))
                        await asyncio.sleep(10)
                        continue
                    self._ws = ws
                    self._connected.set()
                    log.info("Установлено соединение с RPC-мостом хоста.")
                    backoff = 2
                    async for raw in ws:
                        await self._on_message(raw)
            except Exception as exc:  # noqa: BLE001
                self._connected.clear()
                self._ws = None
                # ВАЖНО: разбудить все висящие вызовы немедленно — иначе текущий
                # tool «зависнет» на полный timeout (200 с), хотя мост уже отвалился.
                self._fail_pending(exc)
                # Быстрое переподключение с джиттером (потолок 8 с): транзиентный
                # разрыв (сеть WSL↔Windows, пропущенный ping) должен восстанавливаться
                # почти мгновенно, а не «висеть» красным до 30 с.
                delay = min(backoff, 8) + random.uniform(0, 1.0)
                log.warning("RPC-мост недоступен (%s). Переподключение через %.1f с.", exc, delay)
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, 8)

    def _fail_pending(self, exc: Exception) -> None:
        """Завершить все ожидающие RPC-вызовы ошибкой (при разрыве соединения)."""
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_result({"ok": False,
                                "error": f"RPC-мост разорвал соединение: {exc}"})

    async def _on_message(self, raw: str) -> None:
        # Одно битое сообщение НЕ должно ронять весь recv-цикл (и соединение).
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("RPC-мост прислал нечитаемое сообщение — пропускаю.")
            return
        mtype = msg.get("type")
        if mtype == "rpc_result":
            fut = self._pending.pop(msg.get("id", ""), None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif mtype == "hitl_request":
            # Транслируем запрос подтверждения в дашборд
            await manager.broadcast("hitl", msg)

    async def call(self, action: str, payload: dict[str, Any],
                   timeout: int = 200) -> dict[str, Any]:
        """Выполнить RPC-вызов на хосте и дождаться результата."""
        ws = self._ws
        if not self._connected.is_set() or ws is None:
            return {"ok": False, "error": "RPC-мост хоста не подключён."}
        req_id = f"rpc-{time.time_ns()}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await ws.send(json.dumps(
                {"type": "rpc", "id": req_id, "action": action, "payload": payload},
                ensure_ascii=False,
            ))
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(req_id, None)
            return {"ok": False, "error": f"Не удалось отправить запрос мосту: {exc}"}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"ok": False, "error": "Таймаут ожидания ответа RPC-моста."}

    async def forward_decision(self, approval_id: str, approved: bool) -> None:
        """Передать решение оператора обратно в RPC-мост."""
        if self._ws is not None:
            await self._ws.send(json.dumps({
                "type": "hitl_decision",
                "approval_id": approval_id,
                "approved": approved,
                "operator": "dashboard",
            }))


bridge = HostBridgeClient()


# --------------------------------------------------------------------------- #
# Жизненный цикл приложения
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("Старт ядра JARVIS-OS. Подключаюсь к подсистемам…")
    bridge_task = asyncio.create_task(bridge.run_forever())
    # MCP-серверы поднимаем в фоне (не блокируя старт ядра): инструменты появятся
    # в реестре агента по мере подключения серверов.
    async def _mcp_init() -> None:
        try:
            from orchestrator.agent import start_mcp
            await start_mcp()
        except Exception:  # noqa: BLE001
            log.exception("MCP init failed (работаю без MCP)")
    mcp_task = asyncio.create_task(_mcp_init())
    yield
    bridge_task.cancel()
    mcp_task.cancel()
    try:
        from orchestrator.agent import stop_mcp
        await stop_mcp()
    except Exception:  # noqa: BLE001
        pass
    log.info("Остановка ядра JARVIS-OS.")


app = FastAPI(title="JARVIS-OS Core Controller", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# REST-эндпоинты
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "ts": time.time()})


@app.get("/status")
async def status() -> JSONResponse:
    """Сводный статус подсистем (опрос /health у зависимостей)."""
    async def probe(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as cli:
                r = await cli.get(url)
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    qwen_ok, uitars_ok, audio_ok = await asyncio.gather(
        probe(QWEN_URL.replace("/v1", "/health")),
        probe(UITARS_URL.replace("/v1", "/health")),
        probe(f"{AUDIO_URL}/health"),
    )
    return JSONResponse({
        "core": True,
        "qwen_coder": qwen_ok,
        "ui_tars": uitars_ok,
        "audio": audio_ok,
        "rpc_bridge": bridge._connected.is_set(),
    })


@app.post("/task")
async def submit_task(payload: dict[str, Any]) -> JSONResponse:
    """
    Поставить задачу в LangGraph-оркестратор.
    Прогресс и токены транслируются в каналы /ws/chat и /ws/deploy.
    """
    from orchestrator.graph import run_task  # ленивый импорт графа

    task_text = payload.get("task", "").strip()
    if not task_text:
        return JSONResponse({"ok": False, "error": "Пустая задача."}, status_code=400)

    async def _runner() -> None:
        async for event in run_task(task_text, bridge=bridge):
            await manager.broadcast(event.get("channel", "chat"), event)

    asyncio.create_task(_runner())
    return JSONResponse({"ok": True, "accepted": task_text})


# --------------------------------------------------------------------------- #
# ПУЛЬТ УПРАВЛЕНИЯ (Control Center): сервисы, GPU, модели, LM Studio, конфиг.
# Команды исполняются на ХОСТЕ через защищённый RPC-мост (bridge.exec).
# Рабочий каталог моста = корень проекта, поэтому пути относительные.
# --------------------------------------------------------------------------- #
CONTAINERS = {
    "qwen": "jarvis-vllm-qwen", "uitars": "jarvis-vllm-uitars",
    "audio": "jarvis-audio", "backend": "jarvis-backend", "sandbox": "jarvis-sandbox",
}
COMPOSE = "wsl/docker-compose.agents.yml"
ENV_FILE = "wsl/.env"
PROFILES_FILE = "wsl/profiles.json"
LMSTUDIO_HOST = os.environ.get("JARVIS_LMSTUDIO_HOST", "http://host.docker.internal:1234")
# Тома и контейнеры, которые чистильщик НИКОГДА не трогает.
PROTECTED_PREFIX = "jarvis"


def _safe_token(s: str) -> bool:
    """Допустимое имя docker-объекта/папки (без shell-метасимволов и traversal)."""
    return bool(s) and ".." not in s and all(
        c.isalnum() or c in "-_.:" for c in s)


async def _host_exec(command: str) -> dict[str, Any]:
    """Выполнить команду на хосте через RPC-мост; вернуть {ok, code, out}."""
    res = await bridge.call("exec", {"command": command})
    r = (res or {}).get("result", {})
    return {"ok": res.get("ok", False), "code": r.get("returncode"),
            "out": (r.get("stdout") or "") + (r.get("stderr") or "")}


async def _update_env_vars(updates: dict[str, str]) -> None:
    """Атомарно записать набор переменных в wsl/.env (через RPC-мост)."""
    cfg = await bridge.call("read_file", {"path": ENV_FILE})
    text = (cfg.get("result", {}) or {}).get("stdout", "")
    keys = set(updates)
    lines = [l for l in text.splitlines()
             if not any(l.startswith(k + "=") for k in keys)]
    for k, v in updates.items():
        lines.append(f"{k}={v}")
    await bridge.call("write_file", {"path": ENV_FILE, "content": "\n".join(lines) + "\n"})


@app.get("/api/control/overview")
async def control_overview() -> JSONResponse:
    """Сводка для пульта: сервисы, GPU/VRAM, локальные модели, LM Studio, конфиг."""
    services = await _host_exec(
        'docker ps -a --filter "name=jarvis-" --format "{{.Names}}|{{.State}}|{{.Status}}"')
    gpu = await _host_exec(
        "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu "
        "--format=csv,noheader,nounits")
    models = await _host_exec("cmd /c dir /b data\\models")
    cfg = await bridge.call("read_file", {"path": ENV_FILE})
    cfg_text = (cfg.get("result", {}) or {}).get("stdout", "")
    # LM Studio — список доступных моделей (напрямую с хоста)
    lms_models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=4) as cli:
            r = await cli.get(f"{LMSTUDIO_HOST}/v1/models")
            lms_models = [m["id"] for m in r.json().get("data", [])]
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({
        "services": services["out"],
        "gpu": gpu["out"].strip(),
        "models": [m for m in models["out"].splitlines() if m.strip()],
        "lmstudio_models": lms_models,
        "config": cfg_text,
        "bridge_connected": bridge._connected.is_set(),
    })


@app.post("/api/control/service")
async def control_service(payload: dict[str, Any]) -> JSONResponse:
    """Управление сервисом: start | stop | restart | recreate."""
    svc = payload.get("service", "")
    action = payload.get("action", "")
    if svc not in CONTAINERS:
        return JSONResponse({"ok": False, "error": "Неизвестный сервис."}, status_code=400)
    name = CONTAINERS[svc]
    if action in ("start", "stop", "restart"):
        res = await _host_exec(f"docker {action} {name}")
    elif action == "recreate":
        res = await _host_exec(
            f'docker compose -f {COMPOSE} --env-file {ENV_FILE} up -d --force-recreate '
            f'--no-deps {svc_to_compose(svc)}')
    else:
        return JSONResponse({"ok": False, "error": "Неизвестное действие."}, status_code=400)
    return JSONResponse(res)


def svc_to_compose(svc: str) -> str:
    return {"qwen": "vllm-qwen-coder", "uitars": "vllm-ui-tars", "audio": "audio-layer",
            "backend": "backend", "sandbox": "sandbox"}.get(svc, svc)


@app.get("/api/control/logs/{svc}")
async def control_logs(svc: str, tail: int = 200) -> JSONResponse:
    if svc not in CONTAINERS:
        return JSONResponse({"ok": False, "error": "Неизвестный сервис."}, status_code=400)
    res = await _host_exec(f"docker logs --tail {int(tail)} {CONTAINERS[svc]}")
    return JSONResponse(res)


@app.post("/api/control/config")
async def control_config(payload: dict[str, Any]) -> JSONResponse:
    """Сохранить wsl/.env (редактор конфигурации в дашборде)."""
    content = payload.get("content", "")
    res = await bridge.call("write_file", {"path": ENV_FILE, "content": content})
    return JSONResponse({"ok": res.get("ok", False), "result": res.get("result")})


@app.post("/api/control/model")
async def control_model(payload: dict[str, Any]) -> JSONResponse:
    """
    Управление моделями: download (скачать репозиторий в data/models/<name>) или
    set (назначить сервису локальную модель и пересоздать контейнер).
    """
    action = payload.get("action", "")
    if action == "download":
        repo = payload.get("repo", "").strip()
        name = payload.get("name", "").strip() or repo.split("/")[-1]
        if not repo:
            return JSONResponse({"ok": False, "error": "Не указан repo."}, status_code=400)
        # Фоновая загрузка на хосте (не блокируем мост); прогресс — в окне процесса
        res = await _host_exec(
            f'start "hf-download" python hf_downloader.py {repo} --dest data\\models\\{name}')
        return JSONResponse({"ok": res["ok"], "started": True, "out": res["out"]})
    if action == "set":
        svc = payload.get("service", "")
        path = payload.get("model_path", "").strip()  # напр. /models/qwen-coder-14b
        env_key = {"qwen": "JARVIS_QWEN_MODEL_PATH",
                   "uitars": "JARVIS_UITARS_MODEL_PATH"}.get(svc)
        if not env_key or not path:
            return JSONResponse({"ok": False, "error": "Нужны service и model_path."},
                                status_code=400)
        updates: dict[str, str] = {env_key: path}
        # Для диспетчера (qwen) можно заменить «мозг» на другую модель, задав
        # квантование/тип/VRAM-профиль (иначе fp16-модель не запустится с AWQ).
        if svc == "qwen":
            if "quantization" in payload:
                q = str(payload.get("quantization") or "").strip().lower()
                updates["JARVIS_QWEN_QUANT_ARGS"] = (
                    "" if q in ("", "none", "fp16", "auto") else f"--quantization {q}")
            if payload.get("dtype"):
                updates["JARVIS_QWEN_DTYPE"] = str(payload["dtype"]).strip()
            if payload.get("gpu_util"):
                updates["JARVIS_QWEN_GPU_UTIL"] = str(payload["gpu_util"]).strip()
            if payload.get("max_len"):
                updates["JARVIS_QWEN_MAX_LEN"] = str(payload["max_len"]).strip()
        await _update_env_vars(updates)
        res = await _host_exec(
            f'docker compose -f {COMPOSE} --env-file {ENV_FILE} up -d --force-recreate '
            f'--no-deps {svc_to_compose(svc)}')
        return JSONResponse(res)
    return JSONResponse({"ok": False, "error": "Неизвестное действие."}, status_code=400)


@app.post("/api/control/stack")
async def control_stack(payload: dict[str, Any]) -> JSONResponse:
    """Управление ВСЕМ стеком сразу: up | down | restart | build."""
    action = payload.get("action", "")
    base = f"docker compose -f {COMPOSE} --env-file {ENV_FILE}"
    # up/build могут быть долгими (сборка/пул образов) → запускаем в отдельном
    # окне на хосте, чтобы не упереться в тайм-аут моста; down/restart — быстрые.
    if action == "up":
        res = await _host_exec(f'start "jarvis-up" cmd /c {base} up -d --remove-orphans')
        return JSONResponse({"ok": res["ok"], "started": True, "out": res["out"]})
    if action == "build":
        res = await _host_exec(f'start "jarvis-build" cmd /c {base} build')
        return JSONResponse({"ok": res["ok"], "started": True, "out": res["out"]})
    if action in ("down", "restart"):
        res = await _host_exec(f"{base} {action}")
        return JSONResponse(res)
    if action == "freevram":
        # Освободить видеопамять: остановить все GPU-контейнеры JARVIS.
        res = await _host_exec(f"{base} stop vllm-qwen-coder vllm-ui-tars audio-layer")
        return JSONResponse(res)
    return JSONResponse({"ok": False, "error": "Неизвестное действие."}, status_code=400)


@app.post("/api/control/lmstudio")
async def control_lmstudio(payload: dict[str, Any]) -> JSONResponse:
    """Управление LM Studio: load <model> | unload (через CLI lms на хосте)."""
    action = payload.get("action", "")
    if action == "unload":
        res = await _host_exec("lms unload --all")
    elif action == "load":
        model = payload.get("model", "").strip()
        if not model:
            return JSONResponse({"ok": False, "error": "Не указана модель."}, status_code=400)
        res = await _host_exec(f"lms load {model} --yes")
    else:
        return JSONResponse({"ok": False, "error": "Неизвестное действие."}, status_code=400)
    return JSONResponse(res)


# --------------------------------------------------------------------------- #
# ПРОФИЛИ СИСТЕМЫ (диспетчер + GUI одним пресетом) — wsl/profiles.json
# --------------------------------------------------------------------------- #
async def _read_profiles() -> dict[str, Any]:
    res = await bridge.call("read_file", {"path": PROFILES_FILE})
    text = (res.get("result", {}) or {}).get("stdout", "")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data.pop("_comment", None)
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


@app.get("/api/control/profiles")
async def control_profiles() -> JSONResponse:
    """Список профилей + текущий .env (чтобы UI определил активный)."""
    profiles = await _read_profiles()
    cfg = await bridge.call("read_file", {"path": ENV_FILE})
    return JSONResponse({"profiles": profiles,
                         "env": (cfg.get("result", {}) or {}).get("stdout", "")})


@app.post("/api/control/profile")
async def control_profile(payload: dict[str, Any]) -> JSONResponse:
    """Профиль системы: download (скачать обе модели) | apply (применить + пересоздать)."""
    action = payload.get("action", "")
    pid = payload.get("profile", "")
    prof = (await _read_profiles()).get(pid)
    if not prof:
        return JSONResponse({"ok": False, "error": "Профиль не найден."}, status_code=400)
    disp, gui = prof.get("dispatcher", {}), prof.get("gui", {})

    if action == "download":
        started = []
        for part in (disp, gui):
            repo, name = part.get("repo", ""), part.get("name", "")
            if repo and _safe_token(name):
                await _host_exec(
                    f'start "hf-{name}" python hf_downloader.py {repo} --dest data\\models\\{name}')
                started.append(f"{repo} → data/models/{name}")
        return JSONResponse({"ok": True, "started": True, "downloads": started})

    if action == "apply":
        env = {**disp.get("env", {}), **gui.get("env", {})}
        await _update_env_vars(env)
        base = f"docker compose -f {COMPOSE} --env-file {ENV_FILE}"
        # Стоп GPU-сервисов (освободить VRAM) → ПОСЛЕДОВАТЕЛЬНЫЙ старт: диспетчер
        # с --wait (дождаться полной загрузки!) → затем UI-TARS → аудио/ядро.
        # Это обязательно: второй vLLM-инстанс должен профилировать память уже
        # после первого, иначе оба не помещаются (кумулятивный util).
        seq = (
            f'{base} stop vllm-qwen-coder vllm-ui-tars audio-layer & '
            f'{base} up -d --wait --wait-timeout 900 --force-recreate --no-deps vllm-qwen-coder && '
            f'{base} up -d --wait --wait-timeout 600 --force-recreate --no-deps vllm-ui-tars & '
            f'{base} up -d audio-layer backend sandbox'
        )
        await _host_exec(f'start "jarvis-profile" cmd /c "{seq}"')
        return JSONResponse({"ok": True, "applied": pid, "started": True})

    return JSONResponse({"ok": False, "error": "Неизвестное действие."}, status_code=400)


# --------------------------------------------------------------------------- #
# СИСТЕМНЫЙ ЧИСТИЛЬЩИК — поиск и удаление мусора Docker / неиспользуемых моделей
# Защита: НИКОГДА не трогает объекты с префиксом 'jarvis' (контейнеры), но тома
# весов (jarvis-models/jarvis-hf) ВНУТРИ умеет чистить по выбору пользователя —
# именно там копятся дубликаты моделей (sync копирует data/models → ext4-том),
# поэтому папка проекта и раздувается.
# --------------------------------------------------------------------------- #
def _env_var(env_text: str, key: str) -> str:
    for line in (env_text or "").splitlines():
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


async def _du_mb(mount_arg: str) -> dict[str, int]:
    """Размеры подпапок (МБ) внутри docker-маунта: имя → МБ (через alpine du)."""
    r = await _host_exec(
        f'docker run --rm {mount_arg} alpine sh -c '
        f'"du -sm /m/* 2>/dev/null | sort -rn"')
    out: dict[str, int] = {}
    for line in (r.get("out") or "").splitlines():
        mt = re.match(r"\s*(\d+)\s+\S*?/([^/\s]+)\s*$", line)
        if mt:
            out[mt.group(2)] = int(mt.group(1))
    return out


@app.post("/api/control/cleanup")
async def control_cleanup(payload: dict[str, Any]) -> JSONResponse:
    """check — найти мусор; clean — удалить выбранное."""
    action = payload.get("action", "")

    if action == "check":
        df = await _host_exec("docker system df")
        dangling = await _host_exec(
            'docker images -f dangling=true --format "{{.ID}}|{{.Size}}"')
        cache = await _host_exec("docker builder du")
        stopped = await _host_exec(
            'docker ps -a --filter status=exited --format "{{.Names}}|{{.Image}}|{{.Status}}"')
        vols = await _host_exec('docker volume ls -f dangling=true --format "{{.Name}}"')
        env_text = (await bridge.call("read_file", {"path": ENV_FILE})
                    ).get("result", {}).get("stdout", "")
        data_dir = _env_var(env_text, "JARVIS_DATA_DIR") or "../data"

        # Размеры: модели-источники (data/models на диске) И их КОПИИ в ext4-томе
        # jarvis-models (главный источник дублей), плюс кэш HF (jarvis-hf).
        data_sizes = await _du_mb(f'-v "{data_dir}/models:/m:ro"')
        vol_sizes = await _du_mb('-v jarvis-models:/m:ro')
        hf = await _host_exec(
            'docker run --rm -v jarvis-hf:/m:ro alpine sh -c '
            '"du -sm /m 2>/dev/null | tail -1"')
        hf_mb = 0
        hfm = re.match(r"\s*(\d+)", hf.get("out") or "")
        if hfm:
            hf_mb = int(hfm.group(1))

        def _referenced(name: str) -> bool:
            return name in env_text

        model_dirs = sorted(data_sizes) or [
            m.strip() for m in (await _host_exec("cmd /c dir /b data\\models"))["out"].splitlines()
            if m.strip()]
        referenced = [d for d in model_dirs if _referenced(d)]
        # дубли/мусор в ext4-томе: модели, которых нет в активном .env
        vol_models = [{"name": n, "mb": mb, "referenced": _referenced(n)}
                      for n, mb in sorted(vol_sizes.items(), key=lambda x: -x[1])]

        anon_volumes = [v.strip() for v in vols["out"].splitlines()
                        if v.strip() and not v.strip().startswith(PROTECTED_PREFIX)]
        stopped_ctrs = [s.strip() for s in stopped["out"].splitlines()
                        if s.strip() and not s.strip().startswith(PROTECTED_PREFIX)]
        dangling_imgs = [d.strip() for d in dangling["out"].splitlines() if d.strip()]
        return JSONResponse({
            "ok": True,
            "df": df["out"].strip(),
            "build_cache": cache["out"].strip(),
            "dangling_images": dangling_imgs,
            "stopped_containers": stopped_ctrs,
            "anon_volumes": anon_volumes,
            "model_dirs": model_dirs,
            "model_sizes_mb": data_sizes,        # data/models: имя → МБ
            "referenced_models": referenced,
            "unused_models": [d for d in model_dirs if not _referenced(d)],
            "vol_models": vol_models,            # КОПИИ в ext4-томе (дубли!) с МБ
            "hf_cache_mb": hf_mb,                # кэш HF (Whisper/Kokoro)
        })

    if action == "clean":
        cats = payload.get("categories", []) or []
        volumes = payload.get("volumes", []) or []
        containers = payload.get("containers", []) or []
        models = payload.get("models", []) or []       # из data/models (диск)
        vol_models = payload.get("vol_models", []) or []  # из ext4-тома jarvis-models
        log_lines: list[str] = []

        async def _run(label: str, cmd: str) -> None:
            r = await _host_exec(cmd)
            log_lines.append(f"$ {label}\n{r['out'].strip()}")

        if "dangling_images" in cats:
            await _run("docker image prune -f", "docker image prune -f")
        if "build_cache" in cats:
            await _run("docker builder prune -f", "docker builder prune -f")
        if "hf_cache" in cats:
            await _run("очистка кэша HF",
                       'docker run --rm -v jarvis-hf:/m alpine sh -c "rm -rf /m/*"')
        for v in volumes:
            if _safe_token(v) and not v.startswith(PROTECTED_PREFIX):
                await _run(f"volume rm {v}", f"docker volume rm {v}")
        for c in containers:
            if _safe_token(c) and not c.startswith(PROTECTED_PREFIX):
                await _run(f"rm {c}", f"docker rm {c}")
        for m in models:
            # rmdir деструктивен → пройдёт HITL-гейт моста (доп. подтверждение).
            if _safe_token(m):
                await _run(f"rmdir models/{m}", f"cmd /c rmdir /s /q data\\models\\{m}")
        for m in vol_models:
            # удаление КОПИИ модели из ext4-тома (главный источник дублей)
            if _safe_token(m):
                await _run(f"том jarvis-models: rm {m}",
                           f'docker run --rm -v jarvis-models:/m alpine rm -rf "/m/{m}"')
        return JSONResponse({"ok": True, "log": "\n\n".join(log_lines) or "Нечего удалять."})

    return JSONResponse({"ok": False, "error": "Неизвестное действие."}, status_code=400)


# --------------------------------------------------------------------------- #
# WS: логи развёртывания
# --------------------------------------------------------------------------- #
# Активные «следящие» процессы docker logs -f (по одному на сервис — без
# дублей при переподключениях дашборда). Живут до завершения процесса backend.
_log_followers: dict[str, asyncio.Task] = {}


@app.websocket("/ws/deploy")
async def ws_deploy(ws: WebSocket) -> None:
    await manager.connect("deploy", ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "channel": "deploy",
                                       "services": list(CONTAINERS.keys())}))
        while True:
            # Дашборд (мониторная) просит подписаться на логи сервисов
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "tail_logs":
                svc = msg.get("service", "qwen")
                _ensure_log_follower(svc)
            elif msg.get("type") == "tail_all":
                for svc in CONTAINERS:
                    _ensure_log_follower(svc)
    except WebSocketDisconnect:
        await manager.disconnect("deploy", ws)


def _ensure_log_follower(svc: str) -> None:
    """Запустить (идемпотентно) слежение за логами контейнера сервиса."""
    container = CONTAINERS.get(svc, svc)
    if svc in _log_followers and not _log_followers[svc].done():
        return
    _log_followers[svc] = asyncio.create_task(_stream_container_logs(svc, container))


async def _stream_container_logs(svc: str, container: str) -> None:
    """
    Стримить логи контейнера в канал deploy (тег = ключ сервиса) через Docker
    Engine API по unix-сокету — без зависимости от бинаря `docker` в контейнере.
    """
    from orchestrator import dockerapi
    try:
        async for line in dockerapi.stream_logs(container, tail=120):
            await manager.broadcast("deploy", {
                "type": "log", "service": svc, "line": line,
            })
    except Exception as exc:  # noqa: BLE001
        await manager.broadcast("deploy", {"type": "log", "service": svc,
                                           "line": f"[не удалось открыть логи: {exc}]"})
    finally:
        _log_followers.pop(svc, None)


# --------------------------------------------------------------------------- #
# WS: универсальный чат с агентом (Telegram-like канал)
# --------------------------------------------------------------------------- #
# Дашборд держит ОДНУ диалоговую сессию; при желании можно мультиплексировать
# по session_id из сообщения. Ход агента сериализуется на сессию (lock), чтобы
# параллельные сообщения не путали оперативный контекст и не плодили нагрузку
# на vLLM (защита от затыков и роста KV-кэша).
_chat_locks: dict[str, asyncio.Lock] = {}
# Текущая выполняемая задача агента по сессии — для аварийной остановки.
_agent_tasks: dict[str, asyncio.Task] = {}


def _chat_lock(session: str) -> asyncio.Lock:
    if session not in _chat_locks:
        _chat_locks[session] = asyncio.Lock()
    return _chat_locks[session]


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    await manager.connect("chat", ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "channel": "chat"}))
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "user_message":
                session = msg.get("session", "default")
                _agent_tasks[session] = asyncio.create_task(_run_agent_turn(
                    msg.get("text", ""), msg.get("id", ""), session))
            elif mtype == "cancel":
                # Аварийная остановка текущей задачи агента.
                session = msg.get("session", "default")
                t = _agent_tasks.get(session)
                if t and not t.done():
                    t.cancel()
                # Закрываем «висящую» реплику, чтобы следующий запрос не утянул
                # прерванную задачу из контекста.
                from orchestrator import agent
                agent.mark_interrupted(session)
                await manager.broadcast("chat", {
                    "id": msg.get("id", ""), "type": "cancelled",
                    "text": "Задача остановлена пользователем."})
            elif mtype == "reset_context":
                from orchestrator import agent
                agent.reset_context(msg.get("session", "default"),
                                    keep_summary=bool(msg.get("keep_summary")))
                await manager.broadcast("chat", {"type": "memory", "event": "reset",
                                                 "text": "Оперативный контекст очищен."})
            elif mtype == "flush_context":
                from orchestrator import agent
                ok = await agent.flush_context(msg.get("session", "default"))
                await manager.broadcast("chat", {"type": "memory", "event": "flushed",
                                                 "text": "Контекст сжат в сводку." if ok
                                                 else "Нечего сжимать."})
    except WebSocketDisconnect:
        await manager.disconnect("chat", ws)


async def _run_agent_turn(user_text: str, msg_id: str, session: str) -> None:
    """Прогнать ход агента и транслировать поток событий в канал chat."""
    from orchestrator.agent import run_chat  # ленивый импорт (синглтоны агента)

    try:
        async with _chat_lock(session):
            try:
                async for ev in run_chat(session, user_text, bridge=bridge):
                    await manager.broadcast("chat", {"id": msg_id, **ev})
            except asyncio.CancelledError:
                # Остановка пользователем: уведомление шлёт обработчик cancel,
                # здесь просто корректно сворачиваемся (lock освобождается).
                raise
            except Exception as exc:  # noqa: BLE001
                await manager.broadcast("chat", {"id": msg_id, "type": "error",
                                                 "error": str(exc)})
    finally:
        if _agent_tasks.get(session) is asyncio.current_task():
            _agent_tasks.pop(session, None)


# --------------------------------------------------------------------------- #
# REST: управление памятью агента (для вкладки «Чат»)
# --------------------------------------------------------------------------- #
@app.get("/api/agent/memory")
async def agent_memory(session: str = "default") -> JSONResponse:
    from orchestrator import agent
    return JSONResponse(agent.memory_overview(session))


@app.post("/api/agent/memory")
async def agent_memory_action(payload: dict[str, Any]) -> JSONResponse:
    """Действия: reset | flush | clear_longterm | save."""
    from orchestrator import agent
    action = payload.get("action", "")
    session = payload.get("session", "default")
    if action == "reset":
        agent.reset_context(session, keep_summary=bool(payload.get("keep_summary")))
        return JSONResponse({"ok": True})
    if action == "flush":
        ok = await agent.flush_context(session)
        return JSONResponse({"ok": ok})
    if action == "clear_longterm":
        n = agent.clear_longterm()
        return JSONResponse({"ok": True, "cleared": n})
    if action == "save":
        text = payload.get("text", "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "Пустая заметка."}, status_code=400)
        tags = payload.get("tags") or []
        item = agent.save_memory(text, tags=tags)
        return JSONResponse({"ok": True, "item": item})
    return JSONResponse({"ok": False, "error": "Неизвестное действие."}, status_code=400)


@app.get("/api/agent/mcp")
async def agent_mcp() -> JSONResponse:
    """Статус MCP-серверов и список их инструментов (для дашборда)."""
    from orchestrator import agent
    return JSONResponse(agent.mcp_status())


# --------------------------------------------------------------------------- #
# WS: аудио-канал (входящие байты → VAD/ASR, исходящие TTS-чанки)
# --------------------------------------------------------------------------- #
@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    """
    Двунаправленный аудио-канал.
    Входящие бинарные сообщения — PCM-чанки микрофона (передаются в аудио-слой
    с VAD и ASR). Исходящие — JSON-транскрипты и бинарные TTS-чанки.
    """
    await manager.connect("audio", ws)
    audio_session = AudioSession(ws)
    try:
        await audio_session.open()
        while True:
            message = await ws.receive()
            if message.get("bytes") is not None:
                await audio_session.feed_audio(message["bytes"])
            elif message.get("text") is not None:
                ctrl = json.loads(message["text"])
                if ctrl.get("type") == "speak":
                    await audio_session.synthesize(ctrl.get("text", ""))
                elif ctrl.get("type") == "end_utterance":
                    await audio_session.flush()
    except WebSocketDisconnect:
        await manager.disconnect("audio", ws)
        await audio_session.close()


class AudioSession:
    """Сессия моста между браузерным WS и аудио-слоем (ASR/TTS) по WebSocket."""

    def __init__(self, client_ws: WebSocket) -> None:
        self.client = client_ws
        self._asr_ws: Optional[Any] = None

    async def open(self) -> None:
        """Открыть соединение к аудио-слою для потокового ASR с VAD."""
        try:
            self._asr_ws = await websockets.connect(
                f"{AUDIO_URL.replace('http', 'ws')}/ws/asr",
                max_size=8 * 1024 * 1024,
            )
            asyncio.create_task(self._pump_transcripts())
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось открыть ASR-канал: %s", exc)

    async def feed_audio(self, chunk: bytes) -> None:
        """Передать PCM-чанк в аудио-слой (там работает VAD)."""
        if self._asr_ws is not None:
            await self._asr_ws.send(chunk)

    async def _pump_transcripts(self) -> None:
        """Получать частичные/финальные транскрипты и слать их клиенту."""
        if self._asr_ws is None:
            return
        try:
            async for raw in self._asr_ws:
                await self.client.send_text(raw if isinstance(raw, str)
                                            else raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            pass

    async def synthesize(self, text: str) -> None:
        """
        Синтез речи Kokoro TTS с НИЗКОЙ ЗАДЕРЖКОЙ: чанки аудио стримятся клиенту
        по мере генерации, не дожидаясь полного предложения.
        """
        try:
            async with httpx.AsyncClient(timeout=None) as cli:
                async with cli.stream("POST", f"{AUDIO_URL}/tts/stream",
                                      json={"text": text}) as resp:
                    await self.client.send_text(json.dumps({"type": "tts_start"}))
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        if chunk:
                            await self.client.send_bytes(chunk)
                    await self.client.send_text(json.dumps({"type": "tts_end"}))
        except Exception as exc:  # noqa: BLE001
            await self.client.send_text(json.dumps({"type": "error", "error": str(exc)}))

    async def flush(self) -> None:
        if self._asr_ws is not None:
            await self._asr_ws.send(json.dumps({"type": "flush"}))

    async def close(self) -> None:
        if self._asr_ws is not None:
            await self._asr_ws.close()


# --------------------------------------------------------------------------- #
# WS: кадры виртуального десктопа (framebuffer UI-TARS → Canvas)
# --------------------------------------------------------------------------- #
@app.websocket("/ws/desktop")
async def ws_desktop(ws: WebSocket) -> None:
    """
    Стрим кадров изолированного виртуального десктопа (Xvfb), где UI-TARS
    двигает курсор, печатает и навигирует приложения. Кадры захватываются
    из Xvfb и передаются как JPEG/PNG-байты для отрисовки на Canvas.
    """
    await manager.connect("desktop", ws)
    streamer = DesktopStreamer()
    try:
        await streamer.start(ws)
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "set_fps":
                streamer.fps = max(1, min(30, int(msg.get("fps", 10))))
            elif msg.get("type") == "input":
                # Проброс ввода оператора в виртуальный десктоп (xdotool)
                await streamer.forward_input(msg)
    except WebSocketDisconnect:
        await manager.disconnect("desktop", ws)
        await streamer.stop()


class DesktopStreamer:
    """Захват кадров Xvfb-десктопа и стрим их в WS-канал."""

    def __init__(self) -> None:
        self.fps = 10
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self, ws: WebSocket) -> None:
        self._running = True
        self._task = asyncio.create_task(self._capture_loop(ws))

    async def _capture_loop(self, ws: WebSocket) -> None:
        """
        Захват кадров через ffmpeg из X11-дисплея (:99 — виртуальный Xvfb).
        Каждый кадр кодируется в JPEG и отправляется как бинарный фрейм.
        """
        display = os.environ.get("JARVIS_XDISPLAY", ":99")
        while self._running:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-loglevel", "error",
                    "-f", "x11grab", "-video_size", "1280x720",
                    "-framerate", str(self.fps), "-i", display,
                    "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                frame, _ = await proc.communicate()
                if frame:
                    await ws.send_bytes(frame)
                await asyncio.sleep(1.0 / max(1, self.fps))
            except Exception as exc:  # noqa: BLE001
                await ws.send_text(json.dumps({"type": "error", "error": str(exc)}))
                await asyncio.sleep(1.0)

    async def forward_input(self, msg: dict[str, Any]) -> None:
        """Проброс мыши/клавиатуры оператора в виртуальный десктоп через xdotool."""
        display = os.environ.get("JARVIS_XDISPLAY", ":99")
        kind = msg.get("input_kind")
        env = {**os.environ, "DISPLAY": display}
        if kind == "move":
            args = ["xdotool", "mousemove", str(msg["x"]), str(msg["y"])]
        elif kind == "click":
            args = ["xdotool", "click", str(msg.get("button", 1))]
        elif kind == "type":
            args = ["xdotool", "type", "--", str(msg.get("text", ""))]
        elif kind == "key":
            args = ["xdotool", "key", "--", str(msg.get("key", ""))]
        else:
            return
        proc = await asyncio.create_subprocess_exec(
            *args, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()


# --------------------------------------------------------------------------- #
# WS: мост HITL (уведомления и решения оператора)
# --------------------------------------------------------------------------- #
@app.websocket("/ws/hitl")
async def ws_hitl(ws: WebSocket) -> None:
    """Канал HITL: получает hitl_request от RPC-моста, шлёт решения оператора."""
    await manager.connect("hitl", ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "hitl_decision":
                await bridge.forward_decision(
                    msg.get("approval_id", ""), bool(msg.get("approved"))
                )
    except WebSocketDisconnect:
        await manager.disconnect("hitl", ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, log_level="info")
