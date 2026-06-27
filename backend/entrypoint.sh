#!/usr/bin/env bash
# entrypoint.sh — старт виртуального десктопа (Xvfb) и ядра JARVIS-OS.
# Xvfb на дисплее :99 даёт изолированный X11-десктоп, кадры которого ядро
# стримит в дашборд (/ws/desktop), а UI-TARS — управляет курсором/вводом.
set -e

export DISPLAY="${JARVIS_XDISPLAY:-:99}"

# Поднимаем виртуальный фреймбуфер 1280x720, если ещё не запущен
if ! pgrep -x Xvfb >/dev/null 2>&1; then
  Xvfb "$DISPLAY" -screen 0 1280x720x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
  sleep 1
fi

# Лёгкий оконный менеджер для предсказуемого расположения окон
if command -v fluxbox >/dev/null 2>&1 && ! pgrep -x fluxbox >/dev/null 2>&1; then
  fluxbox >/tmp/fluxbox.log 2>&1 &
  sleep 1
fi

echo "[entrypoint] Виртуальный десктоп готов на $DISPLAY. Запускаю ядро…"
exec uvicorn server:app --host 0.0.0.0 --port 8000
