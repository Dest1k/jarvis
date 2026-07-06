# JARVIS OS — запуск актуального `main`

Этот документ фиксирует рабочий путь запуска после переноса runtime-исправлений в `main`.

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
docker compose -f wsl/docker-compose.agents.yml --env-file wsl/.env config
```

## 3. Рекомендуемые профили

### Стабильный монолитный старт

```powershell
python jarvis.py up --profile gemma4-mono
```

`gemma4-mono` поднимает одну мультимодальную модель-диспетчер и не запускает отдельный UI-TARS. Это меньше движущихся частей и меньше риск VRAM-конфликтов.

### Безопасный старт без аудио

```powershell
python jarvis.py up --profile gemma4-mono --no-audio
```

### Запасной монолит

```powershell
python jarvis.py up --profile gemma27-mono --no-audio
```

### Классическая раздельная связка

```powershell
python jarvis.py up --profile qwen-classic --no-audio
```

### Просторный fallback при давлении VRAM

```powershell
python jarvis.py up --profile gemma12-tars7 --no-audio
```

## 4. CUDA/UVA hardening

В compose и `.env.example` проброшены безопасные флаги:

```env
CUDA_VISIBLE_DEVICES=0
CUDA_DEVICE_ORDER=PCI_BUS_ID
CUDA_DISABLE_P2P=1
NCCL_P2P_DISABLE=1
```

Они держат видимой только дискретную NVIDIA GPU и отключают peer-to-peer/NCCL P2P пути, которые в WSL2/Docker могут провоцировать `RuntimeError: UVA is not available`.

## 5. Проверка исправления контекста и «почему»

1. Открой dashboard.
2. Отправь запрос, который вызывает инструменты.
3. Открой блок `Ход выполнения` / `почему?`.
4. Нажми `🧠 Память` → `📥 Сжать в сводку и скрыть «почему»`.
5. Текст ответа должен остаться, но runtime trace должен исчезнуть.
6. Нажми `🧹 Очистить контекст и экран`.
7. Текущая вкладка должна очиститься, а `episodic_memory_logs` по session id удаляются backend-обёрткой `orchestrator.reset_context`.
8. Обнови страницу: старые `steps/why` не должны вернуться из `localStorage`.

## 6. MCP и фоновый runtime

Background runtime включается лениво при первом ходе агента и проходит через `orchestrator` entrypoint:

```env
JARVIS_BACKGROUND_RUNTIME=1
JARVIS_IDLE_AFTER_SEC=45
JARVIS_IDLE_INTERVAL_SEC=90
```

Автономные branch/test self-heal циклы по умолчанию выключены:

```env
JARVIS_SELF_HEAL_ENABLE=0
```

Для осознанного включения:

```powershell
$env:JARVIS_SELF_HEAL_ENABLE="1"
```

## 7. Кластер по LAN / Mesh VPN

В `wsl/.env` можно добавить JSON worker-ноды:

```env
JARVIS_CLUSTER_NODES=[{"name":"laptop-5080","base_url":"http://192.168.1.50:8001/v1","model":"qwen-coder","role":"coder","transport":"lan","weight":2}]
```

Для Tailscale/WireGuard указывай mesh IP/hostname в `base_url`, например:

```env
JARVIS_CLUSTER_NODES=[{"name":"laptop-5080-tail","base_url":"http://100.x.y.z:8001/v1","model":"qwen-coder","role":"coder","transport":"tailscale","weight":2}]
```

## 8. Сеть Researcher-Agent

Диагностика сети работает безопасно: HTTP probe внутри контейнера + DNS probe на хосте через PowerShell. Recovery — только opt-in:

```env
JARVIS_NETWORK_RECOVERY_SERVICES=Dnscache
JARVIS_NETWORK_RECOVERY_CMD=
```

JARVIS не меняет сетевые политики сам: оператор явно задаёт разрешённый service restart или собственный recovery hook.

## 9. Диагностика после запуска

```powershell
python jarvis.py status
python jarvis.py diag
```

Открой dashboard:

```text
http://localhost:3000
```
