# Оффлайн-запуск JARVIS

Цель: после первичной загрузки образов, моделей и сборки локальных контейнеров JARVIS должен стартовать без обращения к интернету.

## Что означает `resolve image config for docker-image`

Это шаг Docker/BuildKit, когда Docker пытается получить метаданные образа из registry. Он нужен только если образ отсутствует локально, если используется mutable tag без локального cache, либо если выполняется сборка/обновление, которая проверяет базовые образы.

Для штатного запуска уже скачанного JARVIS этот шаг нежелателен. Нормальный оффлайн-режим: все images уже есть локально, модели лежат в named volumes или в `/models`, а compose не пытается pull-ить registry.

## Первичная подготовка онлайн

Один раз при наличии интернета:

```powershell
# из корня проекта
python jarvis.py up --profile gemma4-mono
```

После успешной загрузки и сборки проверьте, что ключевые образы есть локально:

```powershell
docker image ls
```

Для GPU runtime должны быть доступны как минимум:

- vLLM image из `JARVIS_VLLM_IMAGE`;
- `jarvis/backend:latest`;
- `jarvis/audio-layer:latest`;
- `jarvis/sandbox:latest`.

Модели должны лежать в Docker volume `jarvis-models` и быть видны внутри vLLM-контейнера как `/models/...`.

## Обычный запуск оффлайн

После первичной подготовки можно запускать без интернета через helper:

```powershell
scripts\offline_start.bat
```

Или напрямую:

```powershell
$env:JARVIS_PULL_POLICY = "never"
docker compose -f wsl/docker-compose.agents.yml --env-file wsl/.env up -d --remove-orphans
```

Если образа нет локально, запуск должен упасть явно, а не пытаться молча скачивать его.

## Когда интернет всё ещё понадобится

Интернет нужен только для операций подготовки и обновления:

- `docker pull` нового vLLM-образа;
- пересборка backend/audio/sandbox после изменения Dockerfile или requirements;
- скачивание моделей HuggingFace;
- `docker compose build --pull` или ручной prune, который удалил нужные слои/образы.

## Что не удалять при чистке Docker

Для оффлайн-старта не удаляйте:

- `jarvis-models`;
- `jarvis-hf`;
- `jarvis-vllm-cache`;
- `jarvis/backend:latest`;
- `jarvis/audio-layer:latest`;
- `jarvis/sandbox:latest`;
- выбранный `JARVIS_VLLM_IMAGE`.

Глубокая Docker-чистка может удалить build cache. Это нормально для освобождения места, но следующая пересборка снова потребует интернет.
