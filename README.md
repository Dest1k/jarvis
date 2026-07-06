# JARVIS-OS

Локальная автономная агентская система для Windows 11 / WSL2 / Docker с единым
Gemma 4 dispatcher, когнитивным ядром, mission planning, native host tools,
background self-heal, RAG, lifelong learning и web Command Center.

JARVIS — это не чат-обёртка над LLM. Это операционный агент: он ведёт цели как
проекты, вызывает внутренние роли Researcher/Coder/Critic, работает с хостом через
native-first инструменты, анализирует собственные инциденты и готовит проверяемые
repair candidates.

## Быстрый старт

```powershell
cd D:\jarvis
git fetch origin
git checkout main
git pull origin main

python scripts/smoke_check.py --skip-dashboard
python jarvis.py up --profile gemma4-mono --no-audio
```

Открыть dashboard:

```text
http://localhost:3000
```

После стабильного запуска можно включить быстрый профиль:

```powershell
python jarvis.py up --profile gemma4-turbo --no-audio
```

И затем аудио:

```powershell
python jarvis.py up --profile gemma4-turbo
```

## Активные профили

В `wsl/profiles.json` оставлены только два профиля:

| Profile | Назначение |
|---|---|
| `gemma4-mono` | стабильный cold-start, eager mode, диагностика и baseline |
| `gemma4-turbo` | максимальная скорость, CUDA graphs, прогретый runtime |

Оба профиля используют одну мультимодальную Gemma 4 как единый dispatcher для
диалога, кода, vision reasoning, GUI intent и agent orchestration. Отдельный
GUI-движок больше не является частью активного runtime.

## Основные команды

```powershell
python jarvis.py profiles
python jarvis.py up --profile gemma4-mono --no-audio
python jarvis.py up --profile gemma4-turbo --no-audio
python jarvis.py status
python jarvis.py diag
python jarvis.py freevram
python jarvis.py stop
```

Проверка без запуска vLLM:

```powershell
python scripts/smoke_check.py
```

## Архитектура

```text
Windows host
  └─ windows_rpc_bridge.py  token RPC, HITL, native host operations

Docker / WSL2
  ├─ backend/server.py      FastAPI, WS, GPU telemetry, cognitive API
  ├─ orchestrator/          Core JARVIS, ReAct tools, missions, self-heal
  ├─ cognitive_core/        SQLite WAL, RAG, plans, audit, learning
  ├─ vLLM dispatcher        Gemma 4, OpenAI-compatible API on :8001
  ├─ audio-layer            optional ASR/TTS on :8003
  └─ sandbox                isolated code execution

Dashboard :3000
  ├─ Chat
  ├─ Cognitive/RAG
  ├─ Agent Operations Center
  ├─ Control Panel
  └─ Monitor
```

## Core principles

- **Core identity first:** пользователь всегда общается с JARVIS; роли работают за кулисами.
- **Mission autonomy:** большие цели превращаются в durable project plans.
- **Native-first host control:** WMI/CIM, Win32 HWND и UI Automation до CLI fallback.
- **Observable autonomy:** runtime, MCP, cluster, GPU, missions и incidents видны в `🧭 Операции`.
- **Self-heal with guardrails:** диагностика и patch candidates автоматически, merge/apply — по политике.
- **Lifelong learning:** resolved incidents превращаются в проверенные knowledge patterns.
- **Gemma-only runtime:** активные профили используют Gemma 4 dispatcher.

## Mission autonomy

В чате можно просить:

```text
JARVIS, оформи это как mission plan: <цель>
JARVIS, выполни следующий runnable шаг mission
JARVIS, покажи mission status
JARVIS, сделай learning_tick по результату
```

Инструмент `mission` поддерживает:

```text
plan          создать project plan
execute       выполнить следующую runnable-задачу
status        показать планы и задачи
run_role      вызвать Researcher/Coder/Critic
learning_tick выполнить одну итерацию обучения
```

## Self-heal

Безопасный режим:

```env
JARVIS_SELF_HEAL_ENABLE=1
JARVIS_SELF_HEAL_APPLY_PATCH=0
JARVIS_REPO_PATH=.
```

JARVIS сканирует логи, классифицирует аномалии, создаёт staging branch,
генерирует `.diff`, запускает `git apply --check`, `compileall` и compose config,
пишет report/candidate в `data/jarvis_core/self_heal/` и возвращает worktree на
исходную ветку.

Применение diff внутри staging branch включается отдельно:

```env
JARVIS_SELF_HEAL_APPLY_PATCH=1
```

## Lifelong learning

```env
JARVIS_LIFELONG_LEARNING=1
JARVIS_LEARNING_MINE_INCIDENTS=1
```

В простое система создаёт sysadmin/assistant правила и превращает resolved
incidents в schema-compatible `pattern` узлы cognitive graph с тегом
`incident_recipe` после Critic-gate.

## LAN / Mesh cluster

```env
JARVIS_CLUSTER_NODES=[{"name":"laptop-5080","base_url":"http://192.168.1.50:8001/v1","model":"dispatcher","role":"coder","transport":"lan","weight":2}]
```

Sub-agent role briefs могут уходить на worker-ноды. Это agent-level offload, не
model/tensor splitting.

## Документация

- [`docs/launch_main_runtime.md`](docs/launch_main_runtime.md) — запуск и режимы.
- [`docs/performance.md`](docs/performance.md) — производительность Gemma 4 runtime.
- [`wsl/profiles.json`](wsl/profiles.json) — два активных профиля.

## Проверка после запуска

```powershell
python jarvis.py status
python jarvis.py diag
```

В dashboard проверь вкладку:

```text
🧭 Операции
```
