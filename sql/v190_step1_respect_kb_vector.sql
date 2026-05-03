-- v1.9.0 step 1: добавление embedding-колонок в ai.respect_kb
-- Идемпотентно (IF NOT EXISTS). Индекс создаётся отдельно после backfill.

BEGIN;

ALTER TABLE ai.respect_kb
  ADD COLUMN IF NOT EXISTS embedding vector(1024),
  ADD COLUMN IF NOT EXISTS embedding_model TEXT,
  ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ;

-- Комментарии для документации
COMMENT ON COLUMN ai.respect_kb.embedding IS
  'multilingual-e5-large vector, 1024-dim, normalized for cosine';
COMMENT ON COLUMN ai.respect_kb.embedding_model IS
  'Tag модели: e5-large-v1 — для миграций и аудита';
COMMENT ON COLUMN ai.respect_kb.embedding_updated_at IS
  'Когда embedding был последний раз обновлён';

COMMIT;
