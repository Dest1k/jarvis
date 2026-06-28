#!/usr/bin/env bash
# =============================================================================
#  wsl_setup_orchestrator.sh — оркестратор развёртывания JARVIS-OS внутри WSL2.
#
#  Запускается из bootstrap_installer.py. Отвечает за:
#    1. Проверку базовых зависимостей (docker, nvidia-container-toolkit, python).
#    2. Подготовку каталогов кэша моделей и виртуального фреймбуфера (Xvfb).
#    3. Скачивание/проверку весов моделей (Qwen2.5-Coder, UI-TARS, Whisper, Kokoro).
#    4. Запуск стека через docker compose (vLLM ×2 + audio + sandbox + backend).
#    5. Стриминг логов поднятия сервисов.
#
#  Все сообщения — на русском. Скрипт идемпотентен: повторный запуск безопасен.
# =============================================================================
set -Eeuo pipefail

# --- Цветной вывод --------------------------------------------------------- #
RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[1;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
log()  { printf '%s[ИНФО]%s %s\n'  "$BLU" "$NC" "$*"; }
ok()   { printf '%s[ОК]%s   %s\n'  "$GRN" "$NC" "$*"; }
warn() { printf '%s[ПРЕД]%s %s\n'  "$YLW" "$NC" "$*"; }
err()  { printf '%s[ОШИБ]%s %s\n'  "$RED" "$NC" "$*" >&2; }
die()  { err "$*"; exit 1; }

trap 'err "Сбой на строке $LINENO. Прерывание оркестратора."' ERR

# --- Пути и переменные ----------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.agents.yml"
MODELS_DIR="${JARVIS_MODELS_DIR:-$HOME/.cache/jarvis/models}"
HF_CACHE_DIR="${HF_HOME:-$HOME/.cache/huggingface}"

# Профиль из bootstrap (с безопасными дефолтами; точный расчёт — docs/vram_matrix.md)
export JARVIS_QWEN_GPU_UTIL="${JARVIS_QWEN_GPU_UTIL:-0.45}"
export JARVIS_QWEN_MAX_LEN="${JARVIS_QWEN_MAX_LEN:-16384}"
export JARVIS_UITARS_GPU_UTIL="${JARVIS_UITARS_GPU_UTIL:-0.20}"

# Идентификаторы моделей (HuggingFace)
QWEN_MODEL="${JARVIS_QWEN_MODEL:-Qwen/Qwen2.5-Coder-14B-Instruct-AWQ}"
UITARS_MODEL="${JARVIS_UITARS_MODEL:-bytedance-research/UI-TARS-2B-SFT}"

log "================================================================"
log "JARVIS-OS · оркестратор WSL2 · старт"
log "Репозиторий:        $REPO_DIR"
log "Каталог моделей:    $MODELS_DIR"
log "Qwen gpu_util:      $JARVIS_QWEN_GPU_UTIL  (max_len=$JARVIS_QWEN_MAX_LEN)"
log "UI-TARS gpu_util:   $JARVIS_UITARS_GPU_UTIL"
log "================================================================"

# --- ЭТАП 1: проверка зависимостей ----------------------------------------- #
log "Проверка зависимостей окружения…"
command -v docker >/dev/null 2>&1 || die "docker не найден внутри WSL2 (включите интеграцию Docker Desktop с дистрибутивом)."
docker compose version >/dev/null 2>&1 || die "docker compose (v2) недоступен."

# Определяем движок: Docker Desktop сам обеспечивает GPU и не использует
# локальный systemd-демон, поэтому установку toolkit и его перезапуск
# выполняем ТОЛЬКО для нативного docker внутри WSL.
DOCKER_DESKTOP=0
if docker info 2>/dev/null | grep -qi 'docker desktop'; then
  DOCKER_DESKTOP=1
fi

if [ "$DOCKER_DESKTOP" -eq 1 ]; then
  ok "Движок: Docker Desktop — поддержка GPU предоставляется автоматически, toolkit не требуется."
elif docker info 2>/dev/null | grep -qi 'nvidia'; then
  ok "NVIDIA runtime уже доступен в нативном docker."
else
  warn "Нативный docker без NVIDIA runtime. Устанавливаю nvidia-container-toolkit…"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list" \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo service docker restart 2>/dev/null || true
  sleep 4
fi
ok "Зависимости проверены."

# --- Проверка проброса GPU ------------------------------------------------- #
log "Проверка доступности GPU внутри контейнера…"
if docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi -L >/tmp/jarvis_gpu.txt 2>&1; then
  ok "GPU доступен: $(cat /tmp/jarvis_gpu.txt)"
else
  err "Проброс GPU не работает:"; cat /tmp/jarvis_gpu.txt
  die "Исправьте NVIDIA Container Toolkit и повторите."
fi

# --- ЭТАП 2: каталоги и Xvfb ----------------------------------------------- #
log "Подготовка каталогов кэша и виртуального дисплея…"
mkdir -p "$MODELS_DIR" "$HF_CACHE_DIR" "$HOME/.jarvis/runtime" "$HOME/.jarvis/sandbox"

# Виртуальный фреймбуфер для UI-TARS (изолированный X11-десктоп)
if ! command -v Xvfb >/dev/null 2>&1; then
  warn "Xvfb не установлен — устанавливаю (нужен для виртуального десктопа)…"
  sudo apt-get update -y && sudo apt-get install -y xvfb x11vnc fluxbox xdotool scrot
fi
ok "Каталоги готовы. Xvfb/x11vnc доступны."

# --- ЭТАП 3: предзагрузка весов моделей ------------------------------------ #
log "Проверка/предзагрузка весов моделей (может занять время)…"
if ! python3 -c 'import huggingface_hub' 2>/dev/null; then
  pip3 install --quiet --upgrade "huggingface_hub[cli]" || warn "Не удалось установить huggingface_hub."
fi

prefetch_model() {
  local repo="$1"; local tag="$2"
  log "  → $tag: $repo"
  if huggingface-cli download "$repo" --local-dir "$MODELS_DIR/$tag" \
        --local-dir-use-symlinks False >/dev/null 2>&1; then
    ok "    Загружено: $tag"
  else
    warn "    Не удалось предзагрузить $tag — vLLM скачает при первом запуске."
  fi
}
# Предзагрузка опциональна; vLLM умеет тянуть веса сам при старте контейнера.
prefetch_model "$QWEN_MODEL"   "qwen-coder-14b"   || true
prefetch_model "$UITARS_MODEL" "ui-tars"          || true

# --- ЭТАП 4: запуск стека -------------------------------------------------- #
log "Запуск контейнеризованного стека (docker compose)…"
cd "$SCRIPT_DIR"

export JARVIS_MODELS_DIR="$MODELS_DIR"
export JARVIS_HF_CACHE="$HF_CACHE_DIR"
export JARVIS_QWEN_MODEL="$QWEN_MODEL"
export JARVIS_UITARS_MODEL="$UITARS_MODEL"

docker compose -f "$COMPOSE_FILE" pull --ignore-pull-failures || warn "Часть образов не удалось спулить заранее."
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

# --- ЭТАП 5: ожидание готовности vLLM -------------------------------------- #
wait_health() {
  local name="$1"; local url="$2"; local retries="${3:-60}"
  log "Ожидаю готовности сервиса $name ($url)…"
  for ((i=1; i<=retries; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      ok "$name готов (попытка $i)."
      return 0
    fi
    sleep 10
    printf '.'
  done
  printf '\n'
  warn "$name не ответил за отведённое время — проверьте логи: docker compose logs $name"
  return 1
}

wait_health "vllm-qwen-coder" "http://127.0.0.1:8001/health" 90 || true
wait_health "vllm-ui-tars"    "http://127.0.0.1:8002/health" 60 || true
wait_health "audio-layer"     "http://127.0.0.1:8003/health" 30 || true
wait_health "backend"         "http://127.0.0.1:8000/health" 30 || true

log "Текущий статус контейнеров:"
docker compose -f "$COMPOSE_FILE" ps

ok "================================================================"
ok "Стек JARVIS-OS поднят."
ok "  Qwen-Coder (vLLM):   http://localhost:8001/v1"
ok "  UI-TARS    (vLLM):   http://localhost:8002/v1"
ok "  Audio      (ASR/TTS):http://localhost:8003"
ok "  Backend    (FastAPI):http://localhost:8000"
ok "================================================================"
ok "Логи vLLM в реальном времени: docker compose -f $COMPOSE_FILE logs -f vllm-qwen-coder"
