-- v1.7.1 rollback
BEGIN;
DROP TABLE IF EXISTS ai.report_cache;
DROP TABLE IF EXISTS ai.report_history;
COMMIT;
