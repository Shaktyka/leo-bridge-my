-- ============================================================================
-- Миграция: v1.9.1.1 — расширение FTS (russian + simple + section_path)
-- ИСПРАВЛЕНО: array_to_string -> IMMUTABLE обёртка ai.kb_section_path_text
-- ============================================================================
-- Контекст: array_to_string в Postgres помечена как STABLE (зависимость от
-- collation), поэтому её нельзя использовать в GENERATED ALWAYS AS ... STORED
-- (требует IMMUTABLE). Решение — IMMUTABLE SQL-функция-обёртка.
-- ============================================================================

\set ON_ERROR_STOP on
\timing on

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. Иммутабельная функция склейки массива в строку
-- ---------------------------------------------------------------------------

\echo '→ Создаём IMMUTABLE-обёртку для склейки section_path:'
CREATE OR REPLACE FUNCTION ai.kb_section_path_text(arr text[])
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT COALESCE(
        (SELECT string_agg(elem, ' ') FROM unnest(arr) AS elem),
        ''
    );
$$;

-- Smoke-test:
\echo '   smoke-test функции:'
SELECT ai.kb_section_path_text(ARRAY['Конспект','Отзывы о нас']) AS result;

-- ---------------------------------------------------------------------------
-- 1. Сохраняем checksum старого fts для финальной валидации
-- ---------------------------------------------------------------------------

\echo
\echo '→ Считаем checksum старой fts-колонки (для аудита):'
SELECT
    count(*)                                AS rows_total,
    count(fts) FILTER (WHERE fts IS NOT NULL) AS rows_with_fts,
    md5(string_agg(fts::text, ',' ORDER BY content_id)) AS old_fts_md5
FROM ai.respect_kb;

-- ---------------------------------------------------------------------------
-- 2. Дропаем старый GIN-индекс
-- ---------------------------------------------------------------------------

\echo
\echo '→ Удаляем старый GIN-индекс fts:'
DROP INDEX IF EXISTS ai.respect_kb_fts_idx;

-- ---------------------------------------------------------------------------
-- 3. Дропаем старую generated-колонку
-- ---------------------------------------------------------------------------

\echo '→ Удаляем старую колонку fts:'
ALTER TABLE ai.respect_kb DROP COLUMN fts;

-- ---------------------------------------------------------------------------
-- 4. Создаём новую generated-колонку
-- ---------------------------------------------------------------------------

\echo '→ Создаём новую колонку fts (russian A/B/C + simple D):'
ALTER TABLE ai.respect_kb
    ADD COLUMN fts tsvector
    GENERATED ALWAYS AS (
        -- (A) Заголовок через русский стеммер, высший вес
        setweight(
            to_tsvector('russian'::regconfig, COALESCE(title, '')),
            'A'
        )
        ||
        -- (B) Тело + attachments через русский стеммер
        setweight(
            to_tsvector('russian'::regconfig, COALESCE(indexable_text, '')),
            'B'
        )
        ||
        -- (C) НОВОЕ: путь раздела через русский стеммер.
        -- Раньше "Отзывы о нас" из section_path в fts не попадало.
        setweight(
            to_tsvector(
                'russian'::regconfig,
                ai.kb_section_path_text(section_path)
            ),
            'C'
        )
        ||
        -- (D) НОВОЕ: всё то же самое через simple-конфигурацию.
        -- simple не стеммит, ловит точные слова, имена, аббревиатуры.
        setweight(
            to_tsvector(
                'simple'::regconfig,
                COALESCE(title, '') || ' ' ||
                COALESCE(indexable_text, '') || ' ' ||
                ai.kb_section_path_text(section_path)
            ),
            'D'
        )
    ) STORED;

-- ---------------------------------------------------------------------------
-- 5. Пересоздаём GIN-индекс
-- ---------------------------------------------------------------------------

\echo '→ Создаём новый GIN-индекс:'
CREATE INDEX respect_kb_fts_idx ON ai.respect_kb USING gin (fts);

-- ---------------------------------------------------------------------------
-- 6. Валидация: проблемная карточка 7805975
-- ---------------------------------------------------------------------------

\echo
\echo '============================================================'
\echo '  ВАЛИДАЦИЯ: матчится ли теперь карточка 7805975'
\echo '============================================================'

\echo
\echo '-- 6.1 plainto_tsquery russian (старая логика — всё ещё false):'
SELECT
    content_id,
    (fts @@ plainto_tsquery('russian', 'отзыв ИП Ткалич Игорь Юрьевич')) AS matches_russian
FROM ai.respect_kb
WHERE content_id = 7805975;

\echo
\echo '-- 6.2 plainto_tsquery simple (НОВАЯ логика — должно быть true):'
SELECT
    content_id,
    (fts @@ plainto_tsquery('simple', 'отзыв ИП Ткалич Игорь Юрьевич')) AS matches_simple
FROM ai.respect_kb
WHERE content_id = 7805975;

\echo
\echo '-- 6.3 Объединённый запрос (russian || simple) — должно быть true:'
SELECT
    content_id,
    (
        fts @@ plainto_tsquery('russian', 'отзыв ИП Ткалич Игорь Юрьевич')
        OR
        fts @@ plainto_tsquery('simple', 'отзыв ИП Ткалич Игорь Юрьевич')
    ) AS matches_combined,
    GREATEST(
        ts_rank_cd(fts, plainto_tsquery('russian', 'отзыв ИП Ткалич Игорь Юрьевич')),
        ts_rank_cd(fts, plainto_tsquery('simple',  'отзыв ИП Ткалич Игорь Юрьевич')) * 0.5
    ) AS rank
FROM ai.respect_kb
WHERE content_id = 7805975;

\echo
\echo '-- 6.4 Регрессия: "командировочные расходы":'
SELECT count(*) AS hits_komandirovochnye
FROM ai.respect_kb
WHERE fts @@ plainto_tsquery('russian', 'командировочные расходы');

\echo
\echo '-- 6.5 Регрессия: "налог на прибыль":'
SELECT count(*) AS hits_nalog
FROM ai.respect_kb
WHERE fts @@ plainto_tsquery('russian', 'налог на прибыль');

-- ---------------------------------------------------------------------------
-- 7. Финальный отчёт
-- ---------------------------------------------------------------------------

\echo
\echo '→ Сводка по новой fts-колонке:'
SELECT
    count(*)                                  AS rows_total,
    count(fts) FILTER (WHERE fts IS NOT NULL)  AS rows_with_fts,
    round(avg(length(fts::text))::numeric, 0)  AS avg_fts_text_bytes,
    pg_size_pretty(pg_relation_size('ai.respect_kb_fts_idx')) AS index_size
FROM ai.respect_kb;

\echo
\echo '============================================================'
\echo '  ПРОВЕРКА ПЕРЕД COMMIT:'
\echo '  - Шаг 6.2 matches_simple = t'
\echo '  - Шаг 6.3 matches_combined = t, rank > 0'
\echo '  - Шаги 6.4, 6.5 hits > 0'
\echo ''
\echo '  Если всё ОК → COMMIT;'
\echo '  Иначе       → ROLLBACK;'
\echo '============================================================'

-- ВНИМАНИЕ: транзакция ОТКРЫТА — выполните COMMIT или ROLLBACK вручную.

-- ============================================================================
-- DOWN-миграция (откат). Выполнять отдельной транзакцией:
-- ============================================================================
-- BEGIN;
-- DROP INDEX IF EXISTS ai.respect_kb_fts_idx;
-- ALTER TABLE ai.respect_kb DROP COLUMN fts;
-- ALTER TABLE ai.respect_kb
--     ADD COLUMN fts tsvector
--     GENERATED ALWAYS AS ((
--         setweight(to_tsvector('russian'::regconfig, COALESCE(title, ''::text)), 'A'::"char") ||
--         setweight(to_tsvector('russian'::regconfig, COALESCE(indexable_text, ''::text)), 'B'::"char")
--     )) STORED;
-- CREATE INDEX respect_kb_fts_idx ON ai.respect_kb USING gin (fts);
-- DROP FUNCTION IF EXISTS ai.kb_section_path_text(text[]);
-- COMMIT;
