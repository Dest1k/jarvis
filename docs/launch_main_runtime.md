# JARVIS OS — запуск актуального `main`

Этот документ фиксирует рабочий путь запуска Gemma-only runtime.

## 1. Получить актуальный main

```powershell
cd D:\jarvis
git fetch origin
git checkout main
git pull origin main
```

## 2. Быстрая проверка перед стартом

Самый удобный readiness gate:

```powershell
python smoke_check.py
```

Ручной эквивалент:

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

## 3. Единственные активные профили

Стабильный режим:

```powershell
python jarvis.py up --profile gemma4-mono --no-audio
```

Полный стабильный режим с аудио:

```powershell
python jarvis.py up --profile gemma4-mono
```

Максимальная скорость после успешной проверки mono:

```powershell
python jarvis.py up --profile gemma4-turbo --no-audio
```

Оба профиля используют одну мультимодальную Gemma 4 как Core dispatcher, vision и GUI-reasoning endpoint. Отдельный GUI-движок больше не поднимается.

## 4. Command Center v3

Dashboard получил cinematic shell и command palette:

```text
Ctrl+K / Cmd+K  → команды, навигация, smoke/start snippets
🧭 Операции      → автономия, миссии, MCP, cluster, GPU, incidents
```

Основной UX-принцип: JARVIS должен выглядеть как живая наблюдаемая система, а не как набор логов и кнопок.

## 5. CUDA/UVA hardening

```env
CUDA_VISIBLE_DEVICES=0
CUDA_DEVICE_ORDER=PCI_BUS_ID
CUDA_DISABLE_P2P=1
NCCL_P2P_DISABLE=1
```

Эти флаги держат видимой только дискретную NVIDIA GPU и отключают peer-to-peer/NCCL P2P пути, которые в WSL2/Docker могут провоцировать `RuntimeError: UVA is not available`.

## 6. Native host automation

```text
native_host    WMI/CIM overview, processes, services, events, hardware
native_window  Win32 HWND list/find + focus-free PostMessage text/enter
native_ui      Windows UI Automation tree/find
```

Правило системного промпта: для процессов, служб, событий, железа и окон сначала использовать native tools, а `windows.exec/powershell` — только fallback.

## 7. Mission autonomy

```text
mission plan          создать durable project plan в cognitive DB
mission execute       исполнить следующую runnable-задачу с учётом dependencies
mission status        показать планы/подзадачи
mission run_role      вызвать Researcher/Coder/Critic brief, с cluster offload при наличии worker-ноды
mission learning_tick выполнить одну безопасную итерацию lifelong learning
```

Это позволяет JARVIS вести большие цели как проект: декомпозиция → исполнение → роли → проверка → память, а не пытаться удержать всю работу в одном сообщении.

## 8. Проверка контекста и «почему»

1. Открой dashboard.
2. Отправь запрос, который вызывает инструменты.
3. Открой блок `Ход выполнения` / `почему?`.
4. Нажми `🧠 Память` → `📥 Сжать в сводку и скрыть «почему»`.
5. Текст ответа должен остаться, но runtime trace должен исчезнуть.
6. Нажми `🧹 Очистить контекст и экран`.
7. Обнови страницу: старые `steps/why` не должны вернуться из `localStorage`.

## 9. MCP и background runtime

```env
JARVIS_BACKGROUND_RUNTIME=1
JARVIS_IDLE_AFTER_SEC=45
JARVIS_IDLE_INTERVAL_SEC=90
JARVIS_MCP_RESTART_SEC=20
JARVIS_MCP_START_TIMEOUT=150
JARVIS_MCP_CALL_TIMEOUT=120
JARVIS_REPO_GIT_DIR=../.git
```

MCP supervisor валидирует command/path, показывает warnings в `/api/agent/mcp` и ретраит failed servers. Git MCP получает read-only `.git` mount в `/app/.git` для диагностики кода; patch/branch self-heal выполняется через RPC-мост хоста.

## 10. Self-heal patch candidates

```env
JARVIS_SELF_HEAL_ENABLE=1
JARVIS_REPO_PATH=.
JARVIS_SELF_HEAL_APPLY_PATCH=0
```

`JARVIS_REPO_PATH=.` означает рабочую директорию RPC-моста, обычно `D:\jarvis`. Self-heal создаёт staging branch, генерирует candidate diff, запускает `git apply --check`, `compileall`, `docker compose config`, коммитит только report/diff и возвращает worktree на исходную ветку.

Разрешить применение diff внутри staging branch можно отдельно:

```env
JARVIS_SELF_HEAL_APPLY_PATCH=1
```

## 11. Lifelong Learning

```env
JARVIS_LIFELONG_LEARNING=1
JARVIS_LEARNING_MINE_INCIDENTS=1
```

В простое система создаёт проверенные sysadmin-правила и превращает `resolved_incidents.json` в schema-compatible `pattern`-узлы cognitive graph с тегом `incident_recipe` после Critic-gate.

## 12. Кластер по LAN / Mesh VPN

```env
JARVIS_CLUSTER_NODES=[{"name":"laptop-5080","base_url":"http://192.168.1.50:8001/v1","model":"dispatcher","role":"coder","transport":"lan","weight":2}]
```

Для Tailscale/WireGuard указывай mesh IP/hostname в `base_url`.

## 13. Сеть Researcher-Agent

```env
JARVIS_NETWORK_RECOVERY_SERVICES=Dnscache
JARVIS_NETWORK_RECOVERY_CMD=
```

JARVIS не меняет сетевые политики сам: оператор явно задаёт разрешённый service restart или собственный recovery hook.

## 14. Диагностика после запуска

```powershell
python jarvis.py status
python jarvis.py diag
```

Dashboard:

```text
http://localhost:3000
```

Проверь вкладку `🧭 Операции`.
