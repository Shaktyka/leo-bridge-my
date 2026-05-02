-- v1.7.1: кеш и история шаблонов отчётов
-- Применять под user ai из /opt/ai/bridge:
--   psql "$DATABASE_URL_AI" -f sql/v171_report_tables.sql

BEGIN;

-- ============================================================
-- 1. report_cache — кеш ответов LLM (для competitor_summary)
-- ============================================================
CREATE TABLE IF NOT EXISTS ai.report_cache (
    cache_key   TEXT PRIMARY KEY,    -- "<template>:<param_hash>"
    content_md  TEXT NOT NULL,       -- сохранённый Markdown
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS report_cache_expires_idx
    ON ai.report_cache (expires_at)
    WHERE expires_at IS NOT NULL;

COMMENT ON TABLE  ai.report_cache IS 'v1.7.1: кеш content_md по (шаблон, параметры). TTL.';
COMMENT ON COLUMN ai.report_cache.cache_key  IS 'Ключ "<template>:<sha256(params_json)>"';
COMMENT ON COLUMN ai.report_cache.expires_at IS 'После этого момента запись считается устаревшей';
COMMENT ON COLUMN ai.report_cache.hit_count  IS 'Сколько раз был кеш-хит. 0 = только записан';


-- ============================================================
-- 2. report_history — лог генераций шаблонов
-- ============================================================
CREATE TABLE IF NOT EXISTS ai.report_history (
    id              BIGSERIAL PRIMARY KEY,
    template_name   TEXT NOT NULL,
    params_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    matrix_user_id  TEXT NOT NULL,
    matrix_room_id  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms     INTEGER,
    status          TEXT NOT NULL DEFAULT 'ok',  -- ok | error | empty
    file_size       INTEGER,                     -- байт сгенерированного docx
    error_message   TEXT,
    cache_hit       BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS report_history_created_idx
    ON ai.report_history (created_at DESC);
CREATE INDEX IF NOT EXISTS report_history_user_idx
    ON ai.report_history (matrix_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS report_history_template_idx
    ON ai.report_history (template_name, created_at DESC);

COMMENT ON TABLE  ai.report_history IS 'v1.7.1: лог всех генераций шаблонов отчётов';
COMMENT ON COLUMN ai.report_history.params_json IS 'Параметры рендера, для аналитики (без содержимого)';
COMMENT ON COLUMN ai.report_history.cache_hit   IS 'true если LLM-вызов был обойдён через кеш';
COMMENT ON COLUMN ai.report_history.duration_ms IS 'От начала render до отправки в Matrix';

COMMIT;

-- ============================================================
-- Verification
-- ============================================================
\echo '=== v1.7.1 tables ==='
SELECT tablename FROM pg_tables
WHERE schemaname = 'ai' AND tablename IN ('report_cache','report_history')
ORDER BY tablename;

\echo '=== empty? ==='
SELECT 'report_cache' AS tbl, count(*) FROM ai.report_cache
UNION ALL
SELECT 'report_history', count(*) FROM ai.report_history;
