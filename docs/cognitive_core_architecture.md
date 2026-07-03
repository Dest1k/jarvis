# JARVIS OS — «Когнитивное ядро» (Cognitive Core): техническая спецификация v3

> Статус: **производственный фундамент + спецификация**. Реализованы и покрыты
> тестами: слой БД, приоритет конфигурации (DB>file), zero-latency
> suspend/resume, плагины, схема на 14 таблиц, а также аудио-fallback
> (`--no-audio`) и аппаратные тумблеры vLLM. Остальное (суб-агенты,
> RAG-ingestion, четыре вкладки Next.js, PWA-голос) специфицировано ниже как
> контракты и поэтапный план — это многофазная стройка, а не один коммит.

Кодовая база фундамента: `backend/cognitive_core/` (`schema.sql`, `db.py`,
`config.py`, `models.py`, `suspend.py`, `plugins.py`).

---

## 0. Экзистенциальная директива

Движущий вопрос самосовершенствования агента, вокруг которого якорятся ВСЕ цели
обучения, приобретение навыков и оптимизация инструментов:

> **«Чего мне ещё не хватает, чтобы стать идеальным системным администратором и
> ассистентом?»**

В коде директива хранится как редактируемый промпт-шаблон
`prompt.existential_directive` в `system_settings_and_prompts` (перекрывает
файловый дефолт). Фоновый «Lifelong Learning»-цикл на каждой итерации
формулирует под-цели, привязанные к высокоуровневому системному
администрированию, автоматизации и ассистент-паттернам.

---

## 1. Аудио-слой: диагноз, архитектурный фикс, fallback

### Корневые причины падений при старте
1. **Жёсткая связанность.** `backend` имел `depends_on: audio-layer` — крэш-цикл
   аудио тормозил/ронял старт всего стека. **Фикс:** зависимость убрана, аудио
   выведено из критического пути (`docker-compose.agents.yml`).
2. **Блокирующий I/O в event loop.** Загрузка Whisper/Kokoro — тяжёлая и
   синхронная. **Фикс:** модели грузятся в ВЫДЕЛЕННЫХ `ThreadPoolExecutor`, а не
   в основном цикле; прогрев фоновый, `/health` отвечает сразу
   (`audio_layer.py`).
3. **Отсутствующие OS-зависимости** (libsndfile/espeak/PortAudio) и
   рассинхрон CUDA/cuDNN. **Фикс:** `Dockerfile.audio` ставит
   `libsndfile1 espeak-ng ffmpeg`; при этом сбой импорта локализован — контейнер
   не падает, а рапортует деградацию в `/health.deps` (self-diagnostics).
4. **Конкуренция за VRAM/устройства в Docker/WSL.** Whisper на CUDA рядом с
   диспетчером при 0.85 util. **Фикс:** аудио поднимается ПОСЛЕ диспетчера,
   best-effort, без `--wait` на его healthcheck.

### Fallback `--no-audio` (реализовано)
`jarvis.py` принимает `--no-audio` → `JARVIS_ENABLE_AUDIO=0` (пишется в `wsl/.env`,
пробрасывается в backend). Лаунчер не поднимает `audio-layer` и останавливает
его; `server.py /status` не тратит время на пробу и рапортует `audio_enabled:false`.

### Команды запуска
```powershell
# Полный запуск С аудио (по умолчанию):
python jarvis.py up --profile moe-turbo

# Безопасный fallback БЕЗ аудио (чистый старт, если аудио крашит):
python jarvis.py up --profile moe-turbo --no-audio

# Диагностика при сбоях (рестарты/exit-коды/логи контейнеров):
python jarvis.py diag
```

---

## 2. Аппаратная оптимизация (пиковая скорость на одной GPU)

Тумблеры (в `docker-compose.agents.yml`, управляются через `wsl/.env` / профиль):

| Параметр | Переменная | По умолчанию | Пиковая скорость |
|----------|-----------|--------------|------------------|
| CUDA-графы | `JARVIS_QWEN_ENFORCE_EAGER` | `--enforce-eager` (безопасно) | **пусто** → CUDA graphs ON (быстрее, но дольше/рискованнее старт на sm_120) |
| Доп. флаги | `JARVIS_QWEN_EXTRA_ARGS` | зависит от режима | `--num-scheduler-steps 8` (multi-step; срезает CPU↔GPU-синхронизацию) |
| KV-блок | `--block-size` в EXTRA_ARGS | vLLM auto (16) | `--block-size 32` под длинный контекст |
| Пул seqs | `--max-num-seqs` | 8/16 | ↑ при высокой конкуренции |
| Префикс-кэш | `--enable-prefix-caching` | ON | переиспользование системных промптов агента |
| KV-квант | `JARVIS_QWEN_KV_DTYPE` | `fp8` | fp8 удваивает ёмкость KV |

> Осознанная осторожность: `--num-scheduler-steps`, `--reasoning-parser` и т.п.
> версионно-зависимы — vLLM валидирует их и умирает мгновенно при незнакомом
> значении (полевой инцидент). Поэтому агрессивные флаги — **opt-in через env**,
> а дефолты стека безопасны. Пиковый рецепт: `JARVIS_QWEN_ENFORCE_EAGER=""` +
> `JARVIS_QWEN_EXTRA_ARGS="--num-scheduler-steps 8 --block-size 32"` на свежем
> образе `vllm/vllm-openai:latest`.

### Zero asyncio bottlenecks (принцип, воплощён в коде)
- БД: `sqlite3` в `ThreadPoolExecutor` (`db.py`) — event loop не блокируется.
- Аудио: выделенные пулы ASR/TTS с приоритетной изоляцией (`audio_layer.py`).
- Sandbox/Docker: через Engine API по unix-сокету (`dockerapi.py`), не CLI.
- Сериализация: pydantic v2 (`pydantic-core` на Rust) для WS/фронта.

---

## 3. База данных: миграция и приоритет конфигурации

### Полная миграция файлового состояния → реляционная БД
- СУБД: **SQLite** (встроенная, WAL, zero-config) с опциональным
  vector-расширением; схема **PostgreSQL-совместима** (embedding как BLOB
  float32 + `dim` — не зависит от расширения).
- 14 таблиц (§4). Старое файловое состояние (`resolved_incidents.json`,
  память, навыки) поэтапно проецируется в `semantic_knowledge_graph`,
  `episodic_memory_logs`, реестр файлов.

### Приоритет конфигурации (DB строго перекрывает файл) — реализовано
`config.get_setting(key)` разрешает значение так:
1. `system_settings_and_prompts` где `is_active=1` — **высший приоритет**
   (hot-swap из UI без рестарта);
2. файловый JSON-fallback;
3. явный default.

`set_setting()` пишет override + аудит; `reset_to_file_default()` снимает
`is_active` → снова действует файл. Промпт-шаблоны (`value_type='prompt'`)
разрешаются тем же механизмом (`get_prompt`).

---

## 4. Схема БД (production-grade, human-editable)

Полностью в `backend/cognitive_core/schema.sql`. Ключевые таблицы:

| Таблица | Назначение | Особые поля |
|---------|-----------|-------------|
| `agent_cognitive_state` | Автомат `IDLE/THINKING/TESTING/SUSPENDED/RECOVERING` | `checkpoint` (JSON для resume) |
| `system_settings_and_prompts` | Реестр настроек/промптов | `is_active` (DB-override), `file_default`, `version` |
| `file_attachments_registry` | Uploads + сгенерированные файлы | `sha256` (дедуп), `mime_type`, `ingest_status`, `message_id/task_id` |
| `file_chunks` | Чанки + эмбеддинги для RAG | `embedding` BLOB, `embedding_dim`, `token_count` |
| `semantic_knowledge_graph` | Навыки/правила/знания | `visibility` (private/team/federated), `importance`, `decay_score`, `critic_verdict`, `version` |
| `episodic_memory_logs` | Пошаговые мысли/тесты/ошибки | `decision_trace`, `used_knowledge_ids`, `outcome` |
| `agent_achievements` | Таймлайн вех | `category`, `importance` |
| `audit_changelog` | Иммутабельный лог правок | `before_json`/`after_json` → **откат** |
| `system_health_snapshots` | Здоровье компонентов | `suggested_fix`, `fix_applied` |
| `user_roles_and_permissions` | RBAC | `role`, `autonomy_level` |
| `project_plans` / `project_tasks` | Долгосрочные планы | `depends_on`, `progress`, `assignee` |

**Версионирование и откат** («git для разума»): каждая мутация проходит
`db.mutate_with_audit()` → снимок before/after в `audit_changelog`;
`db.rollback_change(audit_id)` восстанавливает before (сам откат тоже логируется).
Покрыто тестом.

---

## 5. Конвейер загрузки файлов (RAG-ingestion)

Асинхронный, вне event loop (тяжёлые шаги — в пул). Стадии
(`file_attachments_registry.ingest_status`):

```
upload → [pending] проверка целостности + sha256 (дедуп по (sha256, owner))
       → [parsing] MIME-детект (python-magic) → парсер текста/кода
                    (pdf/docx/txt/код → plain text)
       → [embedding] чанкинг (по токенам, overlap) → эмбеддинги → file_chunks
       → [ready]  инъекция в активный RAG-контекст текущей сессии
       → [failed] ingest_error + запись в system_health (self-heal видит)
```

Контракт API: `POST /api/files/upload` (multipart) → немедленно `{file_id,
ingest_status:"pending"}`; прогресс стримится в WS-канал `files`:
`{file_id, stage, percent}`. По `ready` чанки доступны для RAG в этой сессии.

---

## 6. Суб-агенты и оркестрация

Главный **Orchestrator** декомпозирует высокоуровневую цель в `project_tasks`
с зависимостями и раздаёт специализированным фоновым суб-агентам:

| Суб-агент | Роль | Инструменты |
|-----------|------|-------------|
| `Researcher-Agent` | Исследует IT/sysadmin-концепции, веб, доки | web_fetch/search, wikipedia |
| `Coder-Agent` | Пишет/тестирует код и скрипты | run_code, shell (sandbox) |
| `Critic-Agent` | Валидирует безопасность/логику КАЖДОГО навыка/правила перед коммитом | статический разбор + правила |
| `Recovery-Agent` | Анализирует traceback, чинит зависимости, рестартит сервисы | diag, docker, pip/apt |

Критик — обязательный шлюз: новый узел `semantic_knowledge_graph` активируется
(`status='active'`) только при `critic_verdict='approved'`. Плагины —
аналогично (`plugins.registry.approve`).

---

## 7. Zero-Latency Suspend/Resume (реализовано)

`suspend.py::BackgroundLoopHandle`:
1. Приход пользовательского промпта → `suspend(active_user, goal)`:
   - ставит `Event` (воркер видит на следующем `await`),
   - **немедленно закрывает исходящий стрим к vLLM** (`stream_closer`) →
     KV-cache/VRAM освобождаются под ход пользователя,
   - пишет состояние `SUSPENDED` + чекпоинт в БД,
   - при застревании — `task.cancel()`.
2. Пользователь простаивает > idle-порога (`should_resume()`) → `resume()`
   поднимает воркер из чекпоинта БД ровно с места остановки.

Пользовательский ход не ждёт «дочистки» фонового цикла — отсюда zero-latency.

---

## 8. Post-Task Post-Mortem (Experience Harvesting)

По завершении реальной сессии Orchestrator классифицирует задачу:
- **Success** → усиление паттерна: `episodic_memory_logs.outcome='success'`,
  инкремент `use_count`/`importance` связанных узлов графа.
- **Failure** → root-cause анализ (Critic/Recovery): запись `outcome='failure'`,
  генерация корректирующего знания (новый узел `kind='rule'`, `source_trace`
  = decision_trace) и, при повторяемости, инцидент в существующий
  `IncidentLedger`/граф.

---

## 9. Self-Healing и авто-диагностика (частично реализовано)

- Стартовая диагностика: `python jarvis.py diag` (рестарты/exit-коды/логи),
  `audio_layer` `/health.deps` (отчёт зависимостей), лечение WSL-интеграции и
  Docker в `ensure_docker`/`ensure_no_wsl_integration`.
- `system_health_snapshots` копит статусы; **Recovery-Agent** по традиции:
  traceback → гипотеза о причине (нет пакета/сервис лёг) → `suggested_fix`
  (напр. `apt install libsndfile1`, `docker restart`) → применение **с
  HITL-подтверждением** (или авто при достаточном `autonomy_level`).
- Человекочитаемый стартовый отчёт: агрегируется из последних health-снимков
  всех компонентов и подсвечивает нездоровые.

---

## 10. Объяснимость и аудит-трейл (фундамент реализован)

- Каждый шаг агента пишет `episodic_memory_logs` с `decision_trace` и
  `used_knowledge_ids`/`used_file_ids` → на вопрос «почему ты так сделал?»
  система отдаёт цепочку: какие правила/навыки/эпизоды/файлы участвовали.
- Все правки БД (навыки, память, конфиг) → `audit_changelog` с версиями и
  откатом. Контракт: `GET /api/audit?table=&row_id=`, `POST /api/audit/rollback`.

---

## 11. Multi-user и федерация

- `user_roles_and_permissions`: `admin`/`operator`/`auditor` + `autonomy_level`.
- Видимость узлов графа: `private` / `team` (общий командный граф) /
  `federated` (анонимизированные семантические правила, разделяемые между
  инстансами Jarvis OS). Приватные пространства уважаются на уровне `owner_id`.
- Федеративный обмен: экспорт узлов `visibility='federated'` без PII →
  импорт в другой инстанс с пометкой источника.

---

## 12. Контракты Next.js API + WebSocket (с явными состояниями UX)

**Требование реактивности:** каждый мутирующий вызов возвращает поля состояния,
а UI отражает `loading → processing → success/error → active` (спиннеры,
оптимистичные апдейты, дизейбл кнопок).

Общая обёртка ответа:
```jsonc
{ "ok": true, "state": "success", "data": {...}, "audit_id": 42, "error": null }
// state ∈ loading|processing|success|error   (для optimistic UI)
```

### Вкладка «Core Chat» (+ загрузка файлов)
| Метод | Путь | Назначение | Состояния |
|-------|------|-----------|-----------|
| WS | `/ws/chat` | диалог с агентом о его состоянии/успехах/провалах | `thought/tool_call/token/...` |
| POST | `/api/files/upload` | multipart, drag-drop по всей рабочей области | `pending`→WS `files` прогресс % |
| WS | `/ws/files` | прогресс ingestion | `{file_id, stage, percent}` |
| POST | `/api/explain` | «почему ты так сделал?» | трасса узлов графа/файлов |

Фронт: `+`-кнопка, нативный Drag-and-Drop, прогресс-бары с %, карточки-превью
стейджа (просмотр/удаление до отправки).

### Вкладка «Humanized DB Explorer»
| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/api/graph?kind=&status=` | дерево/граф узлов |
| GET | `/api/graph/{id}` | узел + история версий |
| PUT | `/api/graph/{id}` | live-edit (оптимистично; спиннер сохранения) |
| DELETE | `/api/graph/{id}` | архив/удаление с подтверждением |
| GET | `/api/audit?table=&row_id=` | diff-view версий |
| POST | `/api/audit/rollback` | one-click откат |

### Вкладка «Unified DB & Config Admin Browser»
| Метод | Путь | Назначение |
|-------|------|-----------|
| GET | `/api/db/{table}?q=&limit=&offset=` | spreadsheet-вид любой таблицы (вкл. `file_attachments_registry`) |
| PUT | `/api/db/{table}/{id}` | правка строки (аудит) |
| GET/POST | `/api/settings` | «Settings & Prompts Editor» (hot-swap) |
| POST | `/api/settings/reset` | сброс ключа к файловому дефолту (спиннер) |

### Панель «Live Cognitive Stream»
`WS /ws/cognition` — три секции:
`{ past:{successes,failures}, present:{state,thought,current_task},
   future:{learning_queue:[...]} }`, стрим из `agent_cognitive_state` +
`episodic_memory_logs` + `project_tasks`.

### «Team & Health Dashboard»
`WS /ws/health` — онлайн-операторы, статусы компонентов из
`system_health_snapshots`, лента recovery-действий и pending-HITL-подтверждений.

### Долгосрочные планы
`GET /api/plans`, `GET /api/plans/{id}/tasks` — таймлайн с `progress` и
`depends_on`; `PUT /api/plans/{id}/tasks/{tid}` — ручная корректировка.

---

## 13. Mobile Companion & Voice (PWA)

- Веб-приложение — responsive **PWA** (manifest + service worker): офлайн-шелл,
  установка на домашний экран, **push-уведомления** о критических алертах и
  pending-HITL.
- Голос: `WS /ws/audio` (уже есть) — PCM16 вверх (VAD→ASR), TTS-чанки вниз,
  hands-free. Опирается на резильентный аудио-слой (§1); при `--no-audio`
  голосовой канал деградирует, текст работает.
- Критические подтверждения приходят пушем → одобрение прямо с телефона.

---

## 14. Продвинутые фичи ядра

1. **Sleep-Cycle Consolidation:** фоновая рутина в простое — прунит дубли/мусор
   логов, пересобирает эмбеддинги, конденсирует сырой опыт
   (`episodic_memory_logs`) в чистые правила (`semantic_knowledge_graph`).
2. **Critic Validation:** каждый навык/правило до коммита проходит Critic (§6).
3. **Secure Sandbox:** фоновое исполнение — в изолированном Docker-sandbox с
   захватом stdout/stderr для самокоррекции (существующий `dockerapi`/`shell`).
4. **Long-Term Planning:** декомпозиция цели → `project_plans/tasks` с
   зависимостями → таймлайн на дашборде, ручная правка.
5. **Plugin-Based Skills:** `plugins.py` — единый интерфейс `SkillPlugin`,
   горячая регистрация, Critic-гейт, HITL по `danger_level` vs `autonomy_level`.
6. **Memory Prioritization & Forgetting:** `importance`/`decay_score` →
   редкие/устаревшие узлы **архивируются** (`status='archived'`, не удаляются) и
   «вспоминаются» при релевантности.
7. **HITL для критичных действий:** операции над продом требуют явного
   подтверждения в UI («опасное действие»); уровень автономии — per-role.

---

## 15. Что реализовано vs. специфицировано (честная граница)

**Реализовано и протестировано в этом коммите:**
- `cognitive_core/`: схема (14 таблиц), async-БД с аудитом/откатом,
  DB>file конфиг, pydantic v2 модели, suspend/resume, плагин-реестр —
  сквозной тест зелёный.
- Аудио: decoupling + `--no-audio` + graceful-degradation + `/health.deps`.
- Аппаратные тумблеры vLLM (env), `jarvis.py diag`.

**Специфицировано (следующие фазы, требуют интеграции с рантаймом):**
- Проводка `cognitive_core` в `server.py` (эндпоинты §12) и в цикл `agent.py`.
- Суб-агенты как отдельные исполнители, RAG-ingestion пайплайн, sleep-cycle.
- Четыре вкладки Next.js и PWA/service-worker.
- Векторный поиск (sqlite-vec) и федеративный обмен.

Причина границы — честность: это многонедельная стройка. Фундамент заложен
production-grade и проверяем; поверх него фазы наращиваются без переписывания.
