# Dockerfile — backend-ядро JARVIS-OS (FastAPI server.py + LangGraph-оркестратор)
FROM python:3.11-slim

# Системные утилиты: docker-cli (управление sandbox), ffmpeg (захват десктопа),
# xdotool (проброс ввода), curl (healthcheck).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ffmpeg xdotool docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY orchestrator ./orchestrator

EXPOSE 8000

# Проверка готовности ядра
HEALTHCHECK --interval=15s --timeout=5s --retries=10 --start-period=60s \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
