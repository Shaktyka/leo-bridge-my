"""
Унификация ai.ai_knowledge под TableSpec pattern.

По аналогии с δ-этапом для personal_kb:
1. Добавить FTS колонки (fts, embedding_model, embedding_updated_at)
2. Пересоздать embedding 1536→1024-dim (e5-large)
3. Создать IVFFlat index
4. Тестировать через hybrid_search

Объём: 3 чанка (тестовые данные), миграция занимает секунды.

Идемпотентен — можно перезапускать с любого шага.
"""
import asyncio
import asyncpg
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.embedder import encode_passages, vector_literal, MODEL_TAG
from dotenv import load_dotenv
import os

load_dotenv("/opt/ai/bridge/.env")

TABLE = "ai.ai_knowledge"


async def step1_add_columns():
    """Шаг 1: FTS + metadata колонки"""
    print("🔧 Шаг 1: Добавление FTS колонок и metadata")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Проверяем что есть
        existing = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'ai' AND table_name = 'ai_knowledge'
              AND column_name IN ('fts', 'embedding_model', 'embedding_updated_at')
        """)
        existing_names = {row['column_name'] for row in existing}
        
        if len(existing_names) == 3:
            # Также проверим FTS index
            has_index = await conn.fetchval("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'ai_knowledge'
                  AND indexname = 'ai_knowledge_fts_idx'
            """)
            if has_index:
                print("✅ Все колонки и FTS index уже на месте — пропускаем")
                return True
        
        # Добавляем недостающие
        if 'fts' not in existing_names:
            print("   Добавляем FTS колонку (generated)...")
            # Используем title + content (метаданных source мало смысла индексировать)
            await conn.execute("""
                ALTER TABLE ai.ai_knowledge
                ADD COLUMN fts tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('russian', COALESCE(title, '')), 'A') ||
                    setweight(to_tsvector('russian', COALESCE(content, '')), 'B')
                ) STORED
            """)
        
        if 'embedding_model' not in existing_names:
            print("   Добавляем embedding_model...")
            await conn.execute("ALTER TABLE ai.ai_knowledge ADD COLUMN embedding_model TEXT")
        
        if 'embedding_updated_at' not in existing_names:
            print("   Добавляем embedding_updated_at...")
            await conn.execute("ALTER TABLE ai.ai_knowledge ADD COLUMN embedding_updated_at TIMESTAMPTZ")
        
        # FTS индекс
        try:
            print("   Создаём FTS index (gin)...")
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS ai_knowledge_fts_idx 
                ON ai.ai_knowledge USING gin(fts)
            """)
        except Exception as e:
            print(f"   ⚠️  FTS index: {e}")
        
        print("✅ Шаг 1 завершён")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False
    finally:
        await conn.close()


async def step2_recreate_embedding_column():
    """Шаг 2: DROP+ADD embedding колонки 1536→1024"""
    print("\n🔄 Шаг 2: Пересоздание embedding колонки vector(1024)")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Проверяем текущую размерность
        current_dim = await conn.fetchval("""
            SELECT atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            JOIN pg_type t ON a.atttypid = t.oid
            WHERE n.nspname = 'ai'
              AND c.relname = 'ai_knowledge'
              AND a.attname = 'embedding'
              AND t.typname = 'vector'
        """)
        
        print(f"   Текущая размерность embedding: {current_dim}")
        
        if current_dim == 1024:
            print("✅ Уже vector(1024) — пропускаем")
            return True
        
        # Сначала дропнем старый hnsw index (он привязан к 1536)
        print("   Удаляем старый hnsw index...")
        await conn.execute("DROP INDEX IF EXISTS ai.ai_knowledge_embedding_idx")
        
        print("   DROP COLUMN embedding (vector(1536))...")
        await conn.execute("ALTER TABLE ai.ai_knowledge DROP COLUMN IF EXISTS embedding")
        
        print("   ADD COLUMN embedding vector(1024)...")
        await conn.execute("ALTER TABLE ai.ai_knowledge ADD COLUMN embedding vector(1024)")
        
        new_dim = await conn.fetchval("""
            SELECT atttypmod FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            JOIN pg_type t ON a.atttypid = t.oid
            WHERE n.nspname = 'ai' AND c.relname = 'ai_knowledge'
              AND a.attname = 'embedding' AND t.typname = 'vector'
        """)
        print(f"✅ Новая размерность: {new_dim}")
        return new_dim == 1024
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False
    finally:
        await conn.close()


async def step3_backfill_embeddings():
    """Шаг 3: Backfill 3 чанков на e5-large"""
    print("\n🧠 Шаг 3: Backfill embeddings на e5-large")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Сколько уже мигрировано
        migrated = await conn.fetchval("""
            SELECT count(*) FROM ai.ai_knowledge
            WHERE embedding_model = $1 AND embedding IS NOT NULL
        """, MODEL_TAG)
        
        total = await conn.fetchval("""
            SELECT count(*) FROM ai.ai_knowledge WHERE content IS NOT NULL
        """)
        
        print(f"   Прогресс: {migrated}/{total}")
        
        if migrated >= total:
            print("✅ Уже всё мигрировано")
            return True
        
        if total == 0:
            print("⚠️  Нет данных для backfill")
            return True
        
        # Прогрев модели если первый раз
        print("   Прогрев e5-large...")
        start = time.time()
        await encode_passages(["test"])
        print(f"   Модель загружена за {time.time()-start:.1f}s")
        
        # Тут всего 3 записи, обработаем за раз
        rows = await conn.fetch("""
            SELECT id, content, source, title
            FROM ai.ai_knowledge
            WHERE content IS NOT NULL
              AND (embedding_model IS NULL OR embedding_model != $1)
            ORDER BY id
        """, MODEL_TAG)
        
        if not rows:
            print("✅ Все уже обработаны")
            return True
        
        print(f"   Обрабатываем {len(rows)} чанков...")
        
        # Подготавливаем passages
        passages = []
        for row in rows:
            title = row['title'] or ''
            content = row['content'] or ''
            # Title в начало (как для других KB)
            passage = f"{title}\n\n{content}"[:8000] if title else content[:8000]
            passages.append(passage)
        
        # Кодируем все за раз
        embeddings = await encode_passages(passages, batch_size=len(passages))
        
        # Сохраняем
        updates = [
            (vector_literal(emb), MODEL_TAG, row['id'])
            for emb, row in zip(embeddings, rows)
        ]
        
        await conn.executemany("""
            UPDATE ai.ai_knowledge
            SET embedding = $1::vector,
                embedding_model = $2,
                embedding_updated_at = now()
            WHERE id = $3
        """, updates)
        
        print(f"✅ {len(rows)} чанков обновлены")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка backfill: {e}")
        return False
    finally:
        await conn.close()


async def step4_create_vector_index():
    """Шаг 4: IVFFlat index"""
    print("\n🗂️  Шаг 4: Создание vector index")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Проверим есть ли уже
        existing = await conn.fetchval("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ai_knowledge'
              AND indexname = 'ai_knowledge_embedding_idx'
        """)
        
        if existing:
            print("✅ Index уже существует")
            return True
        
        # Для 3 строк — минимальный lists
        # IVFFlat требует data в таблице чтобы выбрать центры кластеров
        # При 3 строках lists=1 (один кластер)
        lists = 1
        
        print(f"   CREATE INDEX ai_knowledge_embedding_idx (IVFFlat, lists={lists})...")
        await conn.execute(f"""
            CREATE INDEX ai_knowledge_embedding_idx
            ON ai.ai_knowledge
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = {lists})
        """)
        
        await conn.execute("ANALYZE ai.ai_knowledge")
        
        print("✅ Index создан")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False
    finally:
        await conn.close()


async def step5_test_hybrid_search():
    """Шаг 5: Smoke-test hybrid_search через TableSpec"""
    print("\n🧪 Шаг 5: Smoke-test через hybrid_search")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        from app.retrieval import hybrid_search, TableSpec
        
        spec = TableSpec(
            name="ai.ai_knowledge",
            pk="id",
            fts_column="fts",
            embedding_column="embedding",
            text_columns=["title", "content"],
        )
        
        # Тестовые запросы по тому что есть в KB
        test_queries = [
            "Leo Anthropic Matrix",
            "Letta agent SDK",
            "Python client",
        ]
        
        print(f"   Spec: {spec.name} pk={spec.pk}")
        
        for query in test_queries:
            print(f"\n   Запрос: '{query}'")
            
            try:
                start = time.time()
                # Без access filter — все доступны (для теста)
                results = await hybrid_search(
                    conn=conn,
                    spec=spec,
                    query=query,
                    access_filter_sql="TRUE",  # без ACL для теста
                    access_filter_params=[],
                    limit=2,
                )
                duration = time.time() - start
                
                print(f"   ✓ {len(results)} результатов за {duration*1000:.0f}ms")
                
                for i, r in enumerate(results, 1):
                    if "cohere_score" in r:
                        score_label = f"cohere={r['cohere_score']:.4f}"
                    elif "rrf_score" in r:
                        score_label = f"rrf={r['rrf_score']:.4f}"
                    else:
                        score_label = "?"
                    
                    # Получаем title для показа
                    info = await conn.fetchrow(
                        "SELECT source, title FROM ai.ai_knowledge WHERE id = $1",
                        r["pk"]
                    )
                    title = (info['title'] or info['source'] or '')[:50]
                    print(f"      {i}. {score_label}  \"{title}\"")
            
            except Exception as e:
                print(f"   ❌ {e}")
                return False
        
        return True
        
    finally:
        await conn.close()


async def main():
    print("🚀 УНИФИКАЦИЯ ai.ai_knowledge")
    print("Третья KB под TableSpec pattern (как respect_kb и personal_kb)")
    print("=" * 60)
    
    steps = [
        ("Колонки FTS + metadata", step1_add_columns),
        ("Embedding 1536→1024", step2_recreate_embedding_column),
        ("Backfill e5-large", step3_backfill_embeddings),
        ("Vector index", step4_create_vector_index),
        ("Smoke-test hybrid_search", step5_test_hybrid_search),
    ]
    
    passed = 0
    for name, step_func in steps:
        ok = await step_func()
        if ok:
            passed += 1
        else:
            print(f"\n❌ Шаг провалился: {name}")
            print(f"   Завершено: {passed}/{len(steps)}")
            return
    
    print("\n" + "=" * 60)
    print(f"✅ ВСЕ {passed}/{len(steps)} ШАГОВ ПРОЙДЕНЫ")
    print()
    print("Архитектурный итог:")
    print("  • respect_kb         (5453) — TableSpec ✓")
    print("  • ai_knowledge_personal (113) — TableSpec ✓")
    print("  • ai_knowledge       (3)    — TableSpec ✓ (СЕЙЧАС)")
    print()
    print("Все три KB унифицированы под единый retrieval.py + e5-large + Cohere.")
    print()
    print("Следующий шаг — задача #3 (γ-этап: query rewriting).")


if __name__ == "__main__":
    asyncio.run(main())
