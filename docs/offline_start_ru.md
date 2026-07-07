# Оффлайн-старт JARVIS

Сообщение Docker/BuildKit вроде `resolve image config for docker-image://...` означает, что Docker пытается получить метаданные образа или Dockerfile frontend из registry. Это нормально при первичной подготовке, но не должно быть обязательным для обычного старта после того, как все образы, модели и зависимости уже прогреты.

## Что изменено для оффлайна

1. В Dockerfile убраны явные строки `# syntax=docker/dockerfile:1`. Они заставляли BuildKit отдельно резолвить frontend-образ `docker/dockerfile` перед сборкой.

2. Для vLLM-сервисов в compose задан `pull_policy: ${JARVIS_PULL_POLICY:-if_not_present}`. Docker не должен тянуть vLLM-образ повторно, если он уже есть локально. Для строгого оффлайн-режима можно поставить:

```env
JARVIS_PULL_POLICY=never
```

3. Добавлен скрипт первичной подготовки:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/offline_prefetch.ps1 -Profile gemma4-mono
```

Он должен выполняться один раз при наличии интернета.

## Что prefetch готовит

- применяет выбранный профиль в `wsl/.env`;
- скачивает Docker images: vLLM, `python:3.11-slim`, `alpine`;
- собирает локальные образы `jarvis/backend`, `jarvis/sandbox`, `jarvis/audio-layer`;
- создаёт named volumes `jarvis-models`, `jarvis-hf`, `jarvis-vllm-cache`;
- синхронизирует модели из `JARVIS_DATA_DIR/models` в Docker volume `jarvis-models`;
- устанавливает `dashboard/node_modules`, если их ещё нет.

## После подготовки

Обычный запуск:

```powershell
python jarvis.py up --profile gemma4-mono
```

Для жёсткого оффлайн-режима добавьте в `wsl/.env`:

```env
JARVIS_PULL_POLICY=never
```

Если при этом образа не хватает, Docker должен не лезть в интернет, а сразу упасть с понятной ошибкой, какой image отсутствует. Тогда нужно временно вернуть интернет и снова выполнить `scripts/offline_prefetch.ps1`.

## Что всё ещё может потребовать интернет

- новая модель, которой нет в `JARVIS_DATA_DIR/models`;
- отсутствующий Docker image;
- пустой `dashboard/node_modules`;
- первая сборка после удаления Docker builder cache;
- очистка Docker volumes через `down -v` или ручное удаление `jarvis-models`, `jarvis-hf`, `jarvis-vllm-cache`.

Если ничего из этого не удалять, JARVIS должен стартовать из локальных образов, локальных volumes и локального builder/npm/pip cache.
