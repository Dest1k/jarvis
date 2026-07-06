# JARVIS OS — запуск актуального `main`

Этот документ фиксирует текущий рабочий путь запуска после переноса runtime-исправлений в `main`.

## 1. Получить актуальный main

```powershell
cd D:\jarvis
git fetch origin
git checkout main
git pull origin main
```

> В репозитории default branch может отличаться от `main`, поэтому checkout `main` обязателен.

## 2. Быстрая проверка перед стартом

```powershell
python -m compileall backend
cd dashboard
npm install --legacy-peer-deps
npm run build
cd ..
python jarvis.py profiles
```

## 3. Рекомендуемые профили

### Стабильный монолитный старт

```powershell
python jarvis.py up --profile gemma4-mono
```

`gemma4-mono` поднимает одну мультимодальную модель-диспетчер и не запускает отдельный UI-TARS. Это меньше движущихся частей и меньше риск VRAM-конфликтов.

### Запасной монолит

```powershell
python jarvis.py up --profile gemma27-mono
```

### Классическая раздельная связка

```powershell
python jarvis.py up --profile qwen-classic
```

### Просторный fallback при давлении VRAM

```powershell
python jarvis.py up --profile gemma12-tars7
```

## 4. Проверка исправления контекста и «почему»

1. Открой dashboard.
2. Отправь запрос, который вызывает инструменты.
3. Открой блок `Ход выполнения` / `почему?`.
4. Нажми `🧠 Память` → `📥 Сжать в сводку и скрыть «почему»`.
5. Текст ответа должен остаться, но runtime trace должен исчезнуть.
6. Нажми `🧹 Очистить контекст и экран`.
7. Текущая вкладка должна очиститься, а `episodic_memory_logs` по session id удаляются backend-обёрткой `orchestrator.reset_context`.
8. Обнови страницу: старые `steps/why` не должны вернуться из `localStorage`.

## 5. MCP и фоновый runtime

MCP-клиент теперь устойчиво переживает падение отдельных серверов и стартует лёгкий background runtime loop. Управление:

```powershell
# Выключить фоновый runtime-loop, если нужен максимально чистый старт
$env:JARVIS_BACKGROUND_RUNTIME="0"

# Включить автономные branch/test self-heal циклы (по умолчанию выключено)
$env:JARVIS_SELF_HEAL_ENABLE="1"
```

## 6. Кластер по LAN / Mesh VPN

В `wsl/.env` можно добавить JSON worker-ноды:

```env
JARVIS_CLUSTER_NODES=[{"name":"laptop-5080","base_url":"http://192.168.1.50:8001/v1","model":"qwen-coder","role":"coder","transport":"lan","weight":2}]
```

Для Tailscale/WireGuard указывай mesh IP/hostname в `base_url`, например:

```env
JARVIS_CLUSTER_NODES=[{"name":"laptop-5080-tail","base_url":"http://100.x.y.z:8001/v1","model":"qwen-coder","role":"coder","transport":"tailscale","weight":2}]
```

## 7. Сеть Researcher-Agent

Диагностика сети работает безопасно: HTTP probe внутри контейнера + DNS probe на хосте через PowerShell. Автоматические recovery-действия только opt-in:

```env
JARVIS_NETWORK_RECOVERY_SERVICES=Dnscache
JARVIS_NETWORK_RECOVERY_CMD=
```

## 8. Важное ограничение

Прямое изменение `backend/orchestrator/inference.py` для удаления `--swap-space 8` было заблокировано safety-фильтром коннектора. Поэтому dense-hybrid режим пока не считается рекомендованным. Для запуска используй профили из раздела 3.
