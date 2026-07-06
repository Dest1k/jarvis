# syntax=docker/dockerfile:1
# Dockerfile — backend-ядро JARVIS-OS (FastAPI server.py + агент-оркестратор + MCP)
FROM python:3.11-slim

# Системные утилиты: docker-cli (управление sandbox), git (GitPython/MCP), ffmpeg,
# xdotool (проброс ввода), Xvfb + fluxbox (виртуальный десктоп), curl (healthcheck),
# nodejs/npm — для TS-based MCP-серверов.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ffmpeg xdotool docker.io \
        xvfb fluxbox x11-utils \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_RETRIES=4 \
    PIP_TIMEOUT=60 \
    PIP_PREFER_BINARY=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GIT_PYTHON_GIT_EXECUTABLE=/usr/bin/git \
    GIT_PYTHON_REFRESH=quiet

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY constraints.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -c constraints.txt \
        mcp mcp-server-fetch mcp-server-time mcp-server-git mcp-server-sqlite \
    || echo "[warn] MCP python-серверы не установились — агент будет без них"
RUN npm install -g --no-fund --no-audit \
        @modelcontextprotocol/server-filesystem \
        @modelcontextprotocol/server-memory \
        @modelcontextprotocol/server-sequential-thinking \
        @modelcontextprotocol/server-everything \
        @playwright/mcp \
        playwright \
    || echo "[warn] MCP npm-серверы не установились"

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN (playwright install-deps chromium || npx --yes playwright install-deps chromium || true) \
    && npx --no-install @playwright/mcp install-browser chromium \
    || echo "[warn] Chromium для Playwright MCP не установился — доустановите вручную"

COPY server.py ./
COPY cognitive_api.py ./
COPY mcp_servers.json ./
COPY orchestrator ./orchestrator
COPY cognitive_core ./cognitive_core
COPY entrypoint.sh ./
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

ENV JARVIS_XDISPLAY=:99
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=10 --start-period=60s \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["./entrypoint.sh"]
