-- ============================================================================
-- Rollback миграции v1.9.1.2: возврат к старой fts-колонке (только russian)
-- ============================================================================
-- Применяется в одной транзакции. Безопасно — fts регенерируется автоматически
-- из generated-выражения. Не требует backfill.
-- ============================================================================

\set ON_ERROR_STOP on
\timing on

BEGIN;

\echo '→ Удаляем GIN-индекс:'
DROP INDEX IF EXISTS ai.respect_kb_fts_idx;

\echo '→ Удаляем расширенную колонку fts:'
ALTER TABLE ai.respect_kb DROP COLUMN fts;

\echo '→ Восстанавливаем старую колонку fts (только russian, A+B):'
ALTER TABLE ai.respect_kb
    ADD COLUMN fts tsvector
    GENERATED ALWAYS AS ((
        setweight(to_tsvector('russian'::regconfig, COALESCE(title, ''::text)), 'A'::"char") ||
        setweight(to_tsvector('russian'::regconfig, COALESCE(indexable_text, ''::text)), 'B'::"char")
    )) STORED;

\echo '→ Пересоздаём GIN-индекс:'
CREATE INDEX respect_kb_fts_idx ON ai.respect_kb USING gin (fts);

\echo '→ Удаляем IMMUTABLE-обёртку:'
DROP FUNCTION IF EXISTS ai.kb_section_path_text(text[]);

\echo
\echo 'Rollback готов. Проверьте, потом COMMIT;'

-- COMMIT;
