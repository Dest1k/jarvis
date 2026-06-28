# syntax=docker/dockerfile:1
# Dockerfile — backend-ядро JARVIS-OS (FastAPI server.py + LangGraph-оркестратор)
FROM python:3.11-slim

# Системные утилиты: docker-cli (управление sandbox), ffmpeg (захват десктопа),
# xdotool (проброс ввода), Xvfb + fluxbox (виртуальный десктоп), curl (healthcheck).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg xdotool docker.io \
        xvfb fluxbox x11-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
# BuildKit-кеш pip → зависимости не качаются заново при пересборках.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY server.py ./
COPY orchestrator ./orchestrator
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV JARVIS_XDISPLAY=:99
EXPOSE 8000

# Проверка готовности ядра
HEALTHCHECK --interval=15s --timeout=5s --retries=10 --start-period=60s \
    CMD curl -fsS http://localhost:8000/health || exit 1

# entrypoint поднимает Xvfb/fluxbox и затем запускает ядро
CMD ["./entrypoint.sh"]
