-- ============================================================
-- AssistantAgent · 数据库结构（Postgres 16 + pgvector）
--
-- 为什么要 pgvector 而不是先用 SQLite：
--   迁移要重建全部向量索引 + 换 embedding 模型全量重算 + 时间语义失真，
--   而记忆是长期资产，迁移一次出一次错，就再也不敢让它"记住"了。
--
-- 初始化： docker compose up -d db
--        psql $DATABASE_URL -f app/db/schema.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------- 任务
CREATE TABLE IF NOT EXISTS app.task (
    id                  uuid PRIMARY KEY,
    session_id          text,
    channel             text,
    input               text NOT NULL,
    state               text NOT NULL DEFAULT 'queued',
    version             integer NOT NULL DEFAULT 1,      -- 乐观锁，取消/确认的竞态靠它
    parent_id           uuid REFERENCES app.task(id),
    cancel_requested_at timestamptz,
    cancel_reason       text,
    model_version       text,                           -- 启动时快照，排查"是不是换模型导致"
    cost_usd            numeric(10, 6) NOT NULL DEFAULT 0,
    steps               integer NOT NULL DEFAULT 0,
    output              text,
    error               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS task_state_idx ON app.task (state);
CREATE INDEX IF NOT EXISTS task_created_idx ON app.task (created_at DESC);

-- ---------------------------------------------------------------- 工具调用
CREATE TABLE IF NOT EXISTS app.tool_call (
    id              bigserial PRIMARY KEY,
    task_id         uuid NOT NULL REFERENCES app.task(id) ON DELETE CASCADE,
    step_no         integer NOT NULL,
    tool            text NOT NULL,
    args            jsonb NOT NULL DEFAULT '{}',
    status          text NOT NULL,                      -- ok | error | denied
    error_code      text,
    side_effect     text NOT NULL DEFAULT 'none',       -- none | reversible | external
    idempotency_key text,                               -- 重试/取消不重复扣款靠它
    latency_ms      integer,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tool_call_task_idx ON app.tool_call (task_id, step_no);

-- ---------------------------------------------------------------- 模型调用
CREATE TABLE IF NOT EXISTS app.llm_call (
    id                bigserial PRIMARY KEY,
    task_id           uuid REFERENCES app.task(id) ON DELETE CASCADE,
    step_no           integer,
    role              text NOT NULL,                    -- intent / rewrite / planner / subagent.work
    model_ref         text NOT NULL,
    model_version     text NOT NULL,
    prompt_tokens     integer,
    completion_tokens integer,
    cached_tokens     integer,
    ttft_ms           integer,
    latency_ms        integer,
    retry             smallint NOT NULL DEFAULT 0,
    degraded          boolean NOT NULL DEFAULT false,   -- 是否走了 fallback
    status            text NOT NULL DEFAULT 'ok',
    error_class       text,
    cost_usd          numeric(10, 6) NOT NULL DEFAULT 0,
    request_blob      text,                             -- sha256 -> 对象存储
    response_blob     text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_call_task_idx ON app.llm_call (task_id, step_no);
CREATE INDEX IF NOT EXISTS llm_call_err_idx ON app.llm_call (created_at DESC) WHERE status <> 'ok';

-- ---------------------------------------------------------------- 权限决策
CREATE TABLE IF NOT EXISTS app.permission_event (
    id         bigserial PRIMARY KEY,
    task_id    uuid REFERENCES app.task(id) ON DELETE CASCADE,
    tool       text NOT NULL,
    decision   text NOT NULL,                           -- ask | allowed | denied
    rule       text,
    decided_by text,                                    -- user | policy
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 通用 trace
CREATE TABLE IF NOT EXISTS app.trace_event (
    id         bigserial PRIMARY KEY,
    task_id    uuid NOT NULL,
    step_no    integer NOT NULL DEFAULT 0,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}',
    at_ms      bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trace_event_task_idx ON app.trace_event (task_id, step_no);

-- ---------------------------------------------------------------- LLM Wiki
CREATE TABLE IF NOT EXISTS app.wiki_page (
    id         bigserial PRIMARY KEY,
    kb_key     text NOT NULL,                           -- me | work | learn | life | ref
    path       text NOT NULL,
    type       text,
    title      text,
    summary    text,                                    -- 给检索用，必须写具体
    body       text,                                    -- 页面正文（权威内容）
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kb_key, path)
);

-- 正文块 + 向量。halfvec 需要 pgvector >= 0.7；老版本改用 vector(1024)
CREATE TABLE IF NOT EXISTS app.kb_chunk (
    id         bigserial PRIMARY KEY,
    kb_key     text NOT NULL,
    page_path  text NOT NULL,
    seq        integer NOT NULL DEFAULT 0,
    content    text NOT NULL,
    metadata   jsonb NOT NULL DEFAULT '{}',
    embedding  halfvec(1024) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_chunk_keys_idx ON app.kb_chunk (kb_key, page_path);
CREATE INDEX IF NOT EXISTS kb_chunk_vec_idx ON app.kb_chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);
