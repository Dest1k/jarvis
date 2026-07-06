# JARVIS OS — запуск актуального `main`

Этот документ фиксирует рабочий путь запуска после переноса runtime-исправлений в `main`.

## 1. Получить актуальный main

```powershell
cd D:\jarvis
git fetch origin
git checkout main
git pull origin main
```

`main` теперь является default branch репозитория.

## 2. Быстрая проверка перед стартом

```powershell
python -m compileall backend
python backend/tests/test_native_runtime.py
python backend/tests/test_agent_system.py
cd dashboard
npm install --legacy-peer-deps
npm run build
cd ..
python jarvis.py profiles
docker compose -f wsl/docker-compose.agents.yml --env-file wsl/.env config
```

## 3. Рекомендуемые профили

```powershell
python jarvis.py up --profile gemma4-mono
```

Безопасный старт без аудио:

```powershell
python jarvis.py up --profile gemma4-mono --no-audio
```

Fallback при VRAM/UVA:

```powershell
python jarvis.py up --profile gemma12-tars7 --no-audio
```

Классическая раздельная связка:

```powershell
python jarvis.py up --profile qwen-classic --no-audio
```

## 4. CUDA/UVA hardening

```env
CUDA_VISIBLE_DEVICES=0
CUDA_DEVICE_ORDER=PCI_BUS_ID
CUDA_DISABLE_P2P=1
NCCL_P2P_DISABLE=1
```

Эти флаги держат видимой только дискретную NVIDIA GPU и отключают peer-to-peer/NCCL P2P пути, которые в WSL2/Docker могут провоцировать `RuntimeError: UVA is not available`.

## 5. Native host automation

```text
native_host    WMI/CIM overview, processes, services, events, hardware
native_window  Win32 HWND list/find + focus-free PostMessage text/enter
native_ui      Windows UI Automation tree/find
```

Правило системного промпта: для процессов, служб, событий, железа и окон сначала использовать native tools, а `windows.exec/powershell` — только fallback.

## 6. Проверка контекста и «почему»

1. Открой dashboard.
2. Отправь запрос, который вызывает инструменты.
3. Открой блок `Ход выполнения` / `почему?`.
4. Нажми `🧠 Память` → `📥 Сжать в сводку и скрыть «почему»`.
5. Текст ответа должен остаться, но runtime trace должен исчезнуть.
6. Нажми `🧹 Очистить контекст и экран`.
7. Обнови страницу: старые `steps/why` не должны вернуться из `localStorage`.

## 7. MCP и background runtime

```env
JARVIS_BACKGROUND_RUNTIME=1
JARVIS_IDLE_AFTER_SEC=45
JARVIS_IDLE_INTERVAL_SEC=90
JARVIS_MCP_RESTART_SEC=20
JARVIS_MCP_START_TIMEOUT=150
JARVIS_MCP_CALL_TIMEOUT=120
```

MCP supervisor валидирует command/path, показывает warnings в `/api/agent/mcp` и ретраит failed servers.

## 8. Self-heal patch candidates

Диагностика включена безопасно. Branch/report режим:

```env
JARVIS_SELF_HEAL_ENABLE=1
JARVIS_REPO_PATH=.
```

`JARVIS_REPO_PATH=.` означает рабочую директорию RPC-моста, обычно `D:\jarvis`. Не ставь `/app`, потому что self-heal git-команды выполняются на Windows-хосте через RPC-мост, а не внутри backend-контейнера.

При включении self-heal:

```text
1. scan logs → classify anomaly;
2. ask LLM for diagnosis;
3. create fix/jarvis-auto-* branch;
4. generate candidate unified diff into data/jarvis_core/self_heal/*.diff;
5. run git apply --check;
6. run compileall + docker compose config;
7. commit report/candidate into staging branch.
```

По умолчанию diff НЕ применяется, только создаётся и проверяется:

```env
JARVIS_SELF_HEAL_APPLY_PATCH=0
```

Для разрешения применения diff в staging branch:

```env
JARVIS_SELF_HEAL_APPLY_PATCH=1
```

Merge/push дальше зависит от Git/HITL политики.

## 9. Lifelong Learning

```env
JARVIS_LIFELONG_LEARNING=1
JARVIS_LEARNING_MINE_INCIDENTS=1
```

В простое система создаёт проверенные sysadmin-правила и превращает `resolved_incidents.json` в `incident_recipe` узлы cognitive graph после Critic-gate.

## 10. Кластер по LAN / Mesh VPN

```env
JARVIS_CLUSTER_NODES=[{"name":"laptop-5080","base_url":"http://192.168.1.50:8001/v1","model":"qwen-coder","role":"coder","transport":"lan","weight":2}]
```

Для Tailscale/WireGuard указывай mesh IP/hostname в `base_url`.

## 11. Сеть Researcher-Agent

```env
JARVIS_NETWORK_RECOVERY_SERVICES=Dnscache
JARVIS_NETWORK_RECOVERY_CMD=
```

JARVIS не меняет сетевые политики сам: оператор явно задаёт разрешённый service restart или собственный recovery hook.

## 12. Диагностика после запуска

```powershell
python jarvis.py status
python jarvis.py diag
```

Dashboard:

```text
http://localhost:3000
```

Проверь вкладку:

```text
🧭 Операции
```
