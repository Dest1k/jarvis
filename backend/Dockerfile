# syntax=docker/dockerfile:1
# Dockerfile — backend-ядро JARVIS-OS (FastAPI server.py + агент-оркестратор + MCP)
FROM python:3.11-slim

# Системные утилиты: docker-cli (управление sandbox), ffmpeg (захват десктопа),
# xdotool (проброс ввода), Xvfb + fluxbox (виртуальный десктоп), curl (healthcheck),
# nodejs/npm — для TS-based MCP-серверов (вендорятся офлайн ниже).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg xdotool docker.io \
        xvfb fluxbox x11-utils \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устойчивость pip к нестабильной сети + возможность указать ЗЕРКАЛО PyPI
# (если прямой доступ к pypi.org рвётся по SSL). Зеркало задаётся build-arg
# PIP_INDEX_URL (в compose — из JARVIS_PIP_INDEX_URL).
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_RETRIES=4 \
    PIP_TIMEOUT=60 \
    PIP_PREFER_BINARY=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt ./
# BuildKit-кеш pip → зависимости не качаются заново при пересборках.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# MCP (ОФФЛАЙН-вендоринг): SDK + python-серверы (fetch/time/git/sqlite) и
# TS-серверы глобально через npm (filesystem/memory/sequential-thinking/
# everything/playwright). Набор 1-в-1 совпадает с mcp_servers.json — все
# серверы зашиты в образ, в рантайме сеть для них не нужна (включаются флагом
# enabled). Сбой установки НЕ ломает образ — агент деградирует и работает без MCP.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install mcp mcp-server-fetch mcp-server-time mcp-server-git mcp-server-sqlite \
    || echo "[warn] MCP python-серверы не установились — агент будет без них"
RUN npm install -g --no-fund --no-audit \
        @modelcontextprotocol/server-filesystem \
        @modelcontextprotocol/server-memory \
        @modelcontextprotocol/server-sequential-thinking \
        @modelcontextprotocol/server-everything \
        @playwright/mcp \
    || echo "[warn] MCP npm-серверы не установились"

COPY server.py ./
COPY mcp_servers.json ./
COPY orchestrator ./orchestrator
COPY entrypoint.sh ./
# Страховка от CRLF: если entrypoint.sh пришёл с Windows-переводами строк
# (git core.autocrlf=true), shebang '…/env bash\r' не выполнится на Linux и
# контейнер уйдёт в Restarting (127). Срезаем \r при сборке — образ всегда LF.
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

ENV JARVIS_XDISPLAY=:99
EXPOSE 8000

# Проверка готовности ядра
HEALTHCHECK --interval=15s --timeout=5s --retries=10 --start-period=60s \
    CMD curl -fsS http://localhost:8000/health || exit 1

# entrypoint поднимает Xvfb/fluxbox и затем запускает ядро
CMD ["./entrypoint.sh"]
