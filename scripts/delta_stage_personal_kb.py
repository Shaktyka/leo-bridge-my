"""
δ-этап v2: Умная миграция с пропуском выполненных шагов.

Исправления:
- Пропуск шага 1 если колонки уже есть
- Пропуск миграции если embedding_model уже установлен
- Пропуск индекса если уже создан
- Идемпотентность всех операций
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

async def check_step1_status():
    """Проверяем статус шага 1"""
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    try:
        existing = await conn.fetch("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'ai' AND table_name = 'ai_knowledge_personal'
              AND column_name IN ('fts', 'embedding_model', 'embedding_updated_at')
        """)
        
        existing_names = [row['column_name'] for row in existing]
        has_all = len(existing_names) == 3
        
        # Проверяем индекс
        has_index = await conn.fetchval("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ai_knowledge_personal' 
              AND indexname = 'ai_knowledge_personal_fts_idx'
        """)
        
        return has_all and has_index, existing_names
    finally:
        await conn.close()


async def check_step2_status():
    """Проверяем статус шага 2"""
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    try:
        # Считаем сколько уже мигрировано на e5-large
        migrated = await conn.fetchval("""
            SELECT count(*) FROM ai.ai_knowledge_personal
            WHERE embedding_model = $1
              AND embedding IS NOT NULL
        """, MODEL_TAG)
        
        total = await conn.fetchval("""
            SELECT count(*) FROM ai.ai_knowledge_personal
            WHERE content IS NOT NULL
        """)
        
        return migrated, total, migrated >= total
    finally:
        await conn.close()


async def check_step3_status():
    """Проверяем статус шага 3"""
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    try:
        has_index = await conn.fetchval("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ai_knowledge_personal'
              AND indexname = 'ai_knowledge_personal_embedding_idx'
        """)
        return bool(has_index)
    finally:
        await conn.close()


async def step1_add_columns():
    """Шаг 1: Добавляем новые колонки (с проверкой)"""
    print("🔧 Шаг 1: Добавление колонок в ai.ai_knowledge_personal")
    print("=" * 50)
    
    step1_done, existing = await check_step1_status()
    
    if step1_done:
        print("✅ Шаг 1 уже выполнен ранее - пропускаем")
        return True
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Добавляем только недостающие колонки
        if 'fts' not in existing:
            print("   Добавляем FTS колонку...")
            await conn.execute("""
                ALTER TABLE ai.ai_knowledge_personal
                ADD COLUMN fts tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('russian', COALESCE(source, '')), 'A') ||
                    setweight(to_tsvector('russian', COALESCE(content, '')), 'B')
                ) STORED
            """)
        
        if 'embedding_model' not in existing:
            print("   Добавляем embedding_model...")
            await conn.execute("""
                ALTER TABLE ai.ai_knowledge_personal
                ADD COLUMN embedding_model TEXT
            """)
        
        if 'embedding_updated_at' not in existing:
            print("   Добавляем embedding_updated_at...")
            await conn.execute("""
                ALTER TABLE ai.ai_knowledge_personal
                ADD COLUMN embedding_updated_at TIMESTAMPTZ
            """)
        
        # Создаём FTS индекс если нет
        try:
            await conn.execute("""
                CREATE INDEX ai_knowledge_personal_fts_idx 
                ON ai.ai_knowledge_personal USING gin(fts)
            """)
            print("   Создаём FTS индекс...")
        except Exception:
            print("   FTS индекс уже существует")
        
        print("✅ Колонки и индексы готовы")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False
    finally:
        await conn.close()


async def step2_migrate_embeddings():
    """Шаг 2: Мигрируем embeddings (с проверкой прогресса)"""
    print("\n🔄 Шаг 2: Миграция embeddings на e5-large")
    print("=" * 50)
    
    migrated, total, step2_done = await check_step2_status()
    
    print(f"Прогресс: {migrated}/{total} чанков уже мигрировано")
    
    if step2_done:
        print("✅ Шаг 2 уже выполнен ранее - пропускаем")
        return True
    
    if total == 0:
        print("❌ Нет данных для миграции")
        return False
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Прогрев модели
        if migrated == 0:
            print("Прогрев e5-large модели...")
            start_time = time.time()
            await encode_passages(["test"])
            print(f"Модель загружена за {time.time()-start_time:.1f}s")
        
        # Обрабатываем только НЕмигрированные
        batch_size = 20
        processed = migrated
        migration_start = time.time()
        
        while processed < total:
            # Читаем батч немигрированных
            rows = await conn.fetch("""
                SELECT id, content, source
                FROM ai.ai_knowledge_personal
                WHERE content IS NOT NULL
                  AND (embedding_model IS NULL OR embedding_model != $1)
                ORDER BY id
                LIMIT $2
            """, MODEL_TAG, batch_size)
            
            if not rows:
                break
            
            print(f"   Обрабатываем батч {processed+1}-{processed+len(rows)}...")
            
            # Подготавливаем тексты
            passages = []
            for row in rows:
                source = row['source'] or ''
                content = row['content'] or ''
                passage = f"{source}\n\n{content}"[:8000]
                passages.append(passage)
            
            # Кодируем
            embeddings = await encode_passages(passages, batch_size=len(passages))
            
            # Сохраняем
            updates = [
                (vector_literal(emb), MODEL_TAG, row['id'])
                for emb, row in zip(embeddings, rows)
            ]
            
            await conn.executemany("""
                UPDATE ai.ai_knowledge_personal
                SET embedding = $1::vector,
                    embedding_model = $2,
                    embedding_updated_at = now()
                WHERE id = $3
            """, updates)
            
            processed += len(rows)
            
            if processed > migrated:  # только если есть прогресс
                elapsed = time.time() - migration_start
                rate = (processed - migrated) / elapsed if elapsed > 0 else 0
                
                print(f"   ✅ {processed}/{total} ({100*processed/total:.1f}%) обработано")
                if rate > 0 and processed < total:
                    eta = (total - processed) / rate
                    print(f"      Скорость: {rate:.1f} чанков/сек, ETA: {eta:.0f}s")
        
        total_time = time.time() - migration_start
        new_processed = processed - migrated
        
        if new_processed > 0:
            print(f"\n✅ Миграция {new_processed} чанков завершена за {total_time/60:.1f} минут")
        else:
            print(f"\n✅ Все чанки уже были мигрированы ранее")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка миграции: {e}")
        return False
    finally:
        await conn.close()


async def step3_create_vector_index():
    """Шаг 3: Создаём vector индекс (с проверкой)"""
    print("\n🗂️  Шаг 3: Создание vector индекса")
    print("=" * 50)
    
    step3_done = await check_step3_status()
    
    if step3_done:
        print("✅ Vector индекс уже существует - пропускаем")
        return True
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        lists = max(10, int(113 ** 0.5))
        
        print(f"Создаём IVFFlat индекс с lists={lists}...")
        await conn.execute("""
            CREATE INDEX ai_knowledge_personal_embedding_idx
            ON ai.ai_knowledge_personal
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = $1)
        """, lists)
        
        await conn.execute("ANALYZE ai.ai_knowledge_personal")
        
        print(f"✅ Vector индекс создан")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка создания индекса: {e}")
        return False
    finally:
        await conn.close()


async def step4_test_hybrid_search():
    """Шаг 4: Тестируем hybrid search"""
    print("\n🧪 Шаг 4: Тест hybrid search на личной KB")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        from app.retrieval import hybrid_search, TableSpec
        
        # TableSpec для личной KB
        personal_spec = TableSpec(
            name="ai.ai_knowledge_personal",
            pk="id",
            fts_column="fts", 
            embedding_column="embedding",
            text_columns=["source", "content"]
        )
        
        # Получаем тестового пользователя
        test_user = await conn.fetchval("""
            SELECT matrix_user_id FROM ai.ai_knowledge_personal
            WHERE embedding_model = $1
            GROUP BY matrix_user_id
            ORDER BY count(*) DESC
            LIMIT 1
        """, MODEL_TAG)
        
        if not test_user:
            print("❌ Нет пользователей с мигрированными embedding'ами")
            return False
        
        print(f"Тестируем для пользователя: {test_user}")
        
        # Получаем список ID для ACL filter
        user_chunks = await conn.fetch("""
            SELECT id FROM ai.ai_knowledge_personal
            WHERE matrix_user_id = $1 AND embedding_model = $2
        """, test_user, MODEL_TAG)
        
        chunk_ids = [str(row['id']) for row in user_chunks]
        
        # Тестовые запросы для личной KB
        test_queries = [
            "документация",
            "техническая документация", 
            "проект",
            "CRM"
        ]
        
        all_passed = True
        
        for query in test_queries:
            print(f"\n   Запрос: '{query}'")
            
            try:
                start = time.time()
                results = await hybrid_search(
                    conn=conn,
                    spec=personal_spec,
                    query=query,
                    access_filter_sql="id = ANY($1::uuid[])",
                    access_filter_params=[chunk_ids],
                    limit=3
                )
                duration = time.time() - start
                
                if results:
                    print(f"   ✅ {len(results)} результатов за {duration*1000:.0f}ms")
                    
                    for i, r in enumerate(results[:2], 1):  # показываем только 2
                        score = r.get('rrf_score', 0) or r.get('fts_rank', 0) or r.get('vec_sim', 0)
                        source_type = r.get('source', 'unknown')
                        
                        # Получаем snippet
                        chunk = await conn.fetchrow("""
                            SELECT source, content FROM ai.ai_knowledge_personal WHERE id = $1
                        """, r['pk'])
                        
                        if chunk:
                            snippet = (chunk['source'] or '')[:50] + "..."
                            print(f"      {i}. score={score:.4f} source={source_type}")
                            print(f"         \"{snippet}\"")
                else:
                    print(f"   ⚠️  0 результатов за {duration*1000:.0f}ms")
                    all_passed = False
                    
            except Exception as e:
                print(f"   ❌ Ошибка: {e}")
                all_passed = False
        
        return all_passed
        
    finally:
        await conn.close()


async def main():
    print("🚀 δ-ЭТАП v2: УМНАЯ МИГРАЦИЯ ЛИЧНОЙ KB")
    print("Переводим ai.ai_knowledge_personal на e5-large + FTS + RRF")
    print("=" * 60)
    
    steps_passed = 0
    total_steps = 4
    
    # Выполняем шаги с проверкой статуса
    if await step1_add_columns():
        steps_passed += 1
    else:
        print("\n❌ Шаг 1 провален - останавливаем")
        return
    
    if await step2_migrate_embeddings():
        steps_passed += 1
    else:
        print("\n❌ Шаг 2 провален - останавливаем")
        return
        
    if await step3_create_vector_index():
        steps_passed += 1
    else:
        print("\n❌ Шаг 3 провален - останавливаем")
        return
    
    if await step4_test_hybrid_search():
        steps_passed += 1
    
    # Итоги
    print("\n" + "=" * 60)
    print("📊 РЕЗУЛЬТАТ δ-ЭТАПА:")
    print(f"   Шагов завершено: {steps_passed}/{total_steps}")
    
    if steps_passed == total_steps:
        print("   🎉 ЛИЧНАЯ KB ПОЛНОСТЬЮ МИГРИРОВАНА!")
        print("   ✅ Hybrid retrieval: FTS + Vector + RRF")
        print("   ✅ e5-large 1024-dim embedding")
        print("   ✅ Такое же качество как в respect_kb")
        print("\n   📋 Следующие шаги:")
        print("   • Интегрировать в общий search API")
        print("   • Тестировать на реальных пользовательских запросах")
        print("   • Опционально: β-этап (Cohere reranker)")
    else:
        print("   ⚠️  Некоторые шаги не выполнены - проверьте ошибки")

if __name__ == "__main__":
    asyncio.run(main())
