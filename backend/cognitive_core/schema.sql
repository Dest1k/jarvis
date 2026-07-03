-- =============================================================================
--  schema.sql — «Когнитивное ядро» JARVIS OS (production-grade, human-editable).
--
--  СУБД: SQLite (стартовая, встроенная, zero-config) с опциональным
--  vector-расширением (sqlite-vec/sqlite-vss). Схема совместима с PostgreSQL
--  (типы приведены к переносимому подмножеству; embedding хранится как BLOB
--  float32-массива + отдельный столбец dim, чтобы не зависеть от расширения).
--
--  Принципы:
--    • Всё редактируется человеком: понятные имена, комментарии, без «магии».
--    • Версионирование и аудит: каждая правка (ручная/авто) уходит в
--      audit_changelog с before/after → откат «как git для разума агента».
--    • DB-override: значения из system_settings_and_prompts с is_active=1
--      перекрывают файловые конфиги в рантайме.
--    • Multi-user: владелец (user_id) + видимость (private/team/federated).
--
--  Идемпотентно: CREATE TABLE IF NOT EXISTS + вставки OR IGNORE.
-- =============================================================================

PRAGMA journal_mode = WAL;      -- конкурентное чтение при записи (низкая латентность)
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;    -- баланс надёжность/скорость на WAL

-- --------------------------------------------------------------------------- --
-- Версия схемы (для миграций)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
    description TEXT
);

-- --------------------------------------------------------------------------- --
-- 1. Пользователи и роли (multi-user, RBAC)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS user_roles_and_permissions (
    user_id      TEXT PRIMARY KEY,             -- uuid/логин
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'operator'
                 CHECK (role IN ('admin', 'operator', 'auditor')),
    -- уровень автономии агента для этого пользователя (0=всё через HITL … 3=макс)
    autonomy_level INTEGER NOT NULL DEFAULT 1 CHECK (autonomy_level BETWEEN 0 AND 3),
    permissions  TEXT NOT NULL DEFAULT '{}',   -- JSON доп. разрешений
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
INSERT OR IGNORE INTO user_roles_and_permissions (user_id, display_name, role, autonomy_level)
VALUES ('local-admin', 'Локальный администратор', 'admin', 2);

-- --------------------------------------------------------------------------- --
-- 2. Состояние когнитивного автомата
--    IDLE → THINKING → TESTING → (SUSPENDED при вводе пользователя) → RECOVERING
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS agent_cognitive_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- единственная строка
    state        TEXT NOT NULL DEFAULT 'IDLE'
                 CHECK (state IN ('IDLE','THINKING','TESTING','SUSPENDED','RECOVERING')),
    prev_state   TEXT,
    -- чекпоинт фонового цикла для zero-latency resume (JSON: цель, шаг, скрэтч)
    checkpoint   TEXT NOT NULL DEFAULT '{}',
    active_goal  TEXT,
    -- active_user — информационное поле (id сессии/пользователя); БЕЗ FK, т.к.
    -- сессия чата не обязана быть зарегистрированным user_id.
    active_user  TEXT,
    updated_at   REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
INSERT OR IGNORE INTO agent_cognitive_state (id, state) VALUES (1, 'IDLE');

-- --------------------------------------------------------------------------- --
-- 3. Настройки и промпт-шаблоны (реестр key-value; DB перекрывает файлы)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS system_settings_and_prompts (
    key         TEXT PRIMARY KEY,             -- напр. 'agent.max_steps', 'prompt.planner'
    value       TEXT NOT NULL,                -- строка/JSON/шаблон промпта
    value_type  TEXT NOT NULL DEFAULT 'string'
                CHECK (value_type IN ('string','int','float','bool','json','prompt')),
    category    TEXT NOT NULL DEFAULT 'general', -- general|limits|model|prompt|hardware
    description TEXT,
    -- is_active=1 → это значение ПЕРЕКРЫВАЕТ файловый конфиг в рантайме
    is_active   INTEGER NOT NULL DEFAULT 1,
    -- значение по умолчанию из файла (для кнопки «сбросить к файловому»)
    file_default TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_by  TEXT REFERENCES user_roles_and_permissions(user_id),
    updated_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_settings_category ON system_settings_and_prompts(category);

-- --------------------------------------------------------------------------- --
-- 4. Реестр файловых вложений (uploads + сгенерированные агентом)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS file_attachments_registry (
    id           TEXT PRIMARY KEY,            -- uuid
    file_name    TEXT NOT NULL,
    storage_ref  TEXT NOT NULL,               -- абсолютный путь / ключ объектного хранилища
    sha256       TEXT NOT NULL,               -- дедуп по хешу
    mime_type    TEXT,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    -- к чему привязан: сообщение чата или фоновая задача
    message_id   TEXT,
    task_id      TEXT,
    session_id   TEXT,
    owner_id     TEXT REFERENCES user_roles_and_permissions(user_id),
    origin       TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('user','agent')),
    -- статус конвейера ingestion (см. file_pipeline)
    ingest_status TEXT NOT NULL DEFAULT 'pending'
                 CHECK (ingest_status IN ('pending','parsing','embedding','ready','failed')),
    ingest_error TEXT,
    token_count  INTEGER,
    chunk_count  INTEGER,
    uploaded_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
-- Дедупликация: один и тот же контент (sha256) у одного владельца не дублируется.
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_sha_owner ON file_attachments_registry(sha256, owner_id);
CREATE INDEX IF NOT EXISTS idx_files_session ON file_attachments_registry(session_id);

-- Чанки файла + их эмбеддинги (RAG). embedding — BLOB float32[dim].
CREATE TABLE IF NOT EXISTS file_chunks (
    id          TEXT PRIMARY KEY,
    file_id     TEXT NOT NULL REFERENCES file_attachments_registry(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    embedding   BLOB,                         -- float32-массив (dim ниже)
    embedding_dim INTEGER,
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON file_chunks(file_id);

-- --------------------------------------------------------------------------- --
-- 5. Семантический граф знаний (навыки, правила, декларативные знания + RAG)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS semantic_knowledge_graph (
    id          TEXT PRIMARY KEY,             -- uuid
    kind        TEXT NOT NULL DEFAULT 'rule'
                CHECK (kind IN ('skill','rule','knowledge','plugin','pattern')),
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,                -- текст правила/навыка/знания
    tags        TEXT NOT NULL DEFAULT '[]',   -- JSON-массив
    embedding   BLOB,
    embedding_dim INTEGER,
    -- владение и видимость (multi-user / federated)
    owner_id    TEXT REFERENCES user_roles_and_permissions(user_id),
    visibility  TEXT NOT NULL DEFAULT 'team'
                CHECK (visibility IN ('private','team','federated')),
    -- приоритизация памяти: важность и распад (для forgetting/archival)
    importance  REAL NOT NULL DEFAULT 0.5,    -- 0..1
    decay_score REAL NOT NULL DEFAULT 1.0,    -- убывает со временем/без использования
    use_count   INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('draft','active','archived','rejected')),
    -- Critic-валидация перед коммитом в активные знания
    critic_verdict TEXT CHECK (critic_verdict IN ('approved','rejected','pending')),
    critic_notes   TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    source_trace TEXT,                        -- откуда выведено (episodic id / файл)
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
    updated_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_skg_kind_status ON semantic_knowledge_graph(kind, status);
CREATE INDEX IF NOT EXISTS idx_skg_owner_vis ON semantic_knowledge_graph(owner_id, visibility);
CREATE INDEX IF NOT EXISTS idx_skg_decay ON semantic_knowledge_graph(decay_score);

-- --------------------------------------------------------------------------- --
-- 6. Эпизодическая память (пошаговые мысли, гипотезы, тесты, ошибки)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS episodic_memory_logs (
    id            TEXT PRIMARY KEY,
    decision_trace TEXT NOT NULL,             -- уникальный id цепочки рассуждений
    session_id    TEXT,
    task_id       TEXT,
    step_index    INTEGER,
    entry_type    TEXT NOT NULL DEFAULT 'thought'
                  CHECK (entry_type IN ('thought','hypothesis','action','observation',
                                        'sandbox_output','error','success','failure')),
    content       TEXT NOT NULL,
    -- ссылки на использованные узлы графа знаний (объяснимость)
    used_knowledge_ids TEXT NOT NULL DEFAULT '[]',  -- JSON-массив id
    used_file_ids TEXT NOT NULL DEFAULT '[]',
    outcome       TEXT CHECK (outcome IN ('success','failure','partial','unknown')),
    created_at    REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_episodic_trace ON episodic_memory_logs(decision_trace);
CREATE INDEX IF NOT EXISTS idx_episodic_session ON episodic_memory_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_episodic_outcome ON episodic_memory_logs(outcome);

-- --------------------------------------------------------------------------- --
-- 7. Достижения (таймлайн вех для дашборда)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS agent_achievements (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    detail      TEXT,
    category    TEXT NOT NULL DEFAULT 'skill', -- skill|milestone|recovery|research
    importance  REAL NOT NULL DEFAULT 0.5,
    achieved_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

-- --------------------------------------------------------------------------- --
-- 8. Аудит-лог (иммутабельный; before/after → откат «git для разума»)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS audit_changelog (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name  TEXT NOT NULL,
    row_id      TEXT NOT NULL,
    op          TEXT NOT NULL CHECK (op IN ('create','update','delete','archive','rollback')),
    actor       TEXT,                         -- user_id или 'agent:<subagent>'
    actor_kind  TEXT NOT NULL DEFAULT 'human' CHECK (actor_kind IN ('human','agent')),
    before_json TEXT,                         -- снимок ДО (для отката)
    after_json  TEXT,                         -- снимок ПОСЛЕ
    reason      TEXT,
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_changelog(table_name, row_id);

-- --------------------------------------------------------------------------- --
-- 9. Снимки здоровья системы (self-healing / auto-diagnostics)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS system_health_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    component   TEXT NOT NULL,                -- vllm-qwen|audio|sandbox|bridge|backend|docker
    status      TEXT NOT NULL CHECK (status IN ('healthy','degraded','down','unknown')),
    detail      TEXT,                         -- traceback / отчёт зависимостей
    -- предложенная Recovery-Agent починка (может требовать HITL)
    suggested_fix TEXT,
    fix_applied INTEGER NOT NULL DEFAULT 0,
    snapshot_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_health_component ON system_health_snapshots(component, snapshot_at);

-- --------------------------------------------------------------------------- --
-- 10. Долгосрочные проекты / планы (декомпозиция целей → таймлайн)
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS project_plans (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    owner_id    TEXT REFERENCES user_roles_and_permissions(user_id),
    status      TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned','in_progress','blocked','done','cancelled')),
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE TABLE IF NOT EXISTS project_tasks (
    id          TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL REFERENCES project_plans(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    assignee    TEXT NOT NULL DEFAULT 'orchestrator', -- orchestrator|researcher|coder|critic
    depends_on  TEXT NOT NULL DEFAULT '[]',   -- JSON-массив id задач-зависимостей
    status      TEXT NOT NULL DEFAULT 'todo'
                CHECK (status IN ('todo','in_progress','blocked','done','failed')),
    progress    REAL NOT NULL DEFAULT 0.0,    -- 0..1 для таймлайна
    result      TEXT,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS idx_ptasks_plan ON project_tasks(plan_id);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (1, 'Первичная схема когнитивного ядра JARVIS OS');
