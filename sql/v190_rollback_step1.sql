-- v1.9.0 step 1 ROLLBACK: убрать embedding-колонки из respect_kb
-- Использовать ТОЛЬКО если что-то пошло не так на этапе α

BEGIN;

DROP INDEX IF EXISTS ai.respect_kb_embedding_idx;

ALTER TABLE ai.respect_kb
  DROP COLUMN IF EXISTS embedding,
  DROP COLUMN IF EXISTS embedding_model,
  DROP COLUMN IF EXISTS embedding_updated_at;

COMMIT;
