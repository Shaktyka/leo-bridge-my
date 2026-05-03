-- v1.9.0 step 1b: IVFFlat vector index для ai.respect_kb
-- Запускать ТОЛЬКО после полного backfill всех embeddings

BEGIN;

CREATE INDEX IF NOT EXISTS respect_kb_embedding_idx
  ON ai.respect_kb
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 80);  -- sqrt(5453) ≈ 74, округлили до 80

ANALYZE ai.respect_kb;

COMMIT;
