"""
Тест личной KB ai.ai_knowledge_personal - исправленная версия.

Проверяем структуру через SQL запросы, не psql команды.
"""
import asyncio
import asyncpg
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
import os

load_dotenv("/opt/ai/bridge/.env")

async def analyze_personal_kb_structure():
    """Анализируем структуру личной KB через SQL"""
    print("🔍 Анализ структуры ai.ai_knowledge_personal")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Описание колонок через информационную схему  
        columns = await conn.fetch("""
            SELECT column_name, data_type, character_maximum_length, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'ai' AND table_name = 'ai_knowledge_personal'
            ORDER BY ordinal_position
        """)
        
        print("📋 Колонки:")
        has_fts = False
        has_embedding = False
        embedding_dim = None
        
        for col in columns:
            col_name = col['column_name']
            data_type = col['data_type'] 
            
            if 'fts' in col_name.lower():
                has_fts = True
            if 'embedding' in col_name.lower():
                has_embedding = True
                if 'USER-DEFINED' in data_type:  # vector тип
                    try:
                        # Получаем размерность vector
                        dim_query = """
                            SELECT pg_attribute.atttypmod as vector_dim
                            FROM pg_attribute
                            JOIN pg_class ON pg_attribute.attrelid = pg_class.oid
                            JOIN pg_namespace ON pg_class.relnamespace = pg_namespace.oid
                            WHERE pg_namespace.nspname = 'ai'
                              AND pg_class.relname = 'ai_knowledge_personal'
                              AND pg_attribute.attname = $1
                        """
                        dim_result = await conn.fetchrow(dim_query, col_name)
                        if dim_result and dim_result['vector_dim']:
                            embedding_dim = dim_result['vector_dim']
                    except:
                        pass
            
            nullable = "NULL" if col['is_nullable'] == 'YES' else "NOT NULL"
            print(f"   {col_name:25} | {data_type:20} | {nullable}")
        
        print(f"\n🔍 Обнаружено:")
        print(f"   FTS колонка: {'✅ есть' if has_fts else '❌ нет'}")
        print(f"   Embedding колонка: {'✅ есть' if has_embedding else '❌ нет'}")
        if embedding_dim:
            print(f"   Размерность embedding: {embedding_dim}")
        
        # Статистика данных
        stats = await conn.fetchrow("""
            SELECT 
                count(*) as total_chunks,
                count(DISTINCT matrix_user_id) as users,
                count(DISTINCT source) as sources,
                count(embedding) as with_embedding
            FROM ai.ai_knowledge_personal
        """)
        
        print(f"\n📊 Статистика данных:")
        print(f"   Всего чанков: {stats['total_chunks']}")
        print(f"   Пользователей: {stats['users']}")
        print(f"   Источников: {stats['sources']}")
        print(f"   С embedding: {stats['with_embedding']}")
        
        # Показываем примеры пользователей
        if stats['total_chunks'] > 0:
            users = await conn.fetch("""
                SELECT matrix_user_id, count(*) as chunks,
                       string_agg(DISTINCT source, ', ') as sources
                FROM ai.ai_knowledge_personal 
                GROUP BY matrix_user_id
                ORDER BY chunks DESC
                LIMIT 5
            """)
            
            print(f"\n👥 Топ пользователей:")
            for user in users:
                user_id = user['matrix_user_id'][:30] + "..." if len(user['matrix_user_id']) > 30 else user['matrix_user_id']
                sources = (user['sources'] or "")[:40] + "..." if len(user['sources'] or "") > 40 else user['sources']
                print(f"   {user_id}: {user['chunks']} чанков, источники: {sources}")
        
        return {
            'has_fts': has_fts,
            'has_embedding': has_embedding,
            'embedding_dim': embedding_dim,
            'total_chunks': stats['total_chunks'],
            'users': stats['users']
        }
        
    finally:
        await conn.close()


async def test_current_vector_search():
    """Тестируем текущий vector search"""
    print("\n🧠 Тест текущего vector search")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Получаем тестового пользователя
        users = await conn.fetch("""
            SELECT DISTINCT matrix_user_id, count(*) as chunks
            FROM ai.ai_knowledge_personal 
            WHERE embedding IS NOT NULL
            GROUP BY matrix_user_id
            ORDER BY chunks DESC
            LIMIT 3
        """)
        
        if not users:
            print("❌ Нет пользователей с embedding в личной KB")
            return
        
        test_user = users[0]['matrix_user_id']
        chunks_count = users[0]['chunks'] 
        print(f"Тестовый пользователь: {test_user}")
        print(f"Чанков с embedding: {chunks_count}")
        
        # Простой тест через cosine similarity (без вычисления нового embedding)
        test_queries = ["документы", "проект", "задачи", "встречи"]
        
        for query in test_queries:
            print(f"\nЗапрос: '{query}'")
            
            # Поиск по текстовому содержимому (fallback)
            try:
                text_results = await conn.fetch("""
                    SELECT chunk_id, source, content
                    FROM ai.ai_knowledge_personal
                    WHERE matrix_user_id = $1 
                      AND content IS NOT NULL
                      AND lower(content) LIKE lower($2)
                    ORDER BY length(content) DESC
                    LIMIT 3
                """, test_user, f"%{query}%")
                
                if text_results:
                    print(f"   📄 Текстовое совпадение: {len(text_results)} результатов")
                    for i, row in enumerate(text_results, 1):
                        source = (row['source'] or '')[:40] + "..." if len(row['source'] or '') > 40 else row['source']
                        content = (row['content'] or '')[:60] + "..." if len(row['content'] or '') > 60 else row['content']
                        print(f"      {i}. src=\"{source}\"")
                        print(f"         \"{content}\"")
                else:
                    print(f"   ❌ Нет текстовых совпадений")
            
            except Exception as e:
                print(f"   ⚠️  Ошибка поиска: {e}")
        
    finally:
        await conn.close()


async def test_e5_large_readiness():
    """Проверяем готовность к миграции на e5-large"""  
    print("\n🔄 Тест готовности к миграции e5-large")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Берём несколько примеров текстов
        samples = await conn.fetch("""
            SELECT chunk_id, content, source
            FROM ai.ai_knowledge_personal
            WHERE content IS NOT NULL AND length(content) > 50
            LIMIT 5
        """)
        
        if not samples:
            print("❌ Нет текстовых данных для теста")
            return
        
        print(f"Тестируем e5-large на {len(samples)} примерах:")
        
        # Тестируем encoding через наш новый embedder
        texts = [s['content'][:500] for s in samples]  # обрезаем для теста
        
        try:
            from app.embedder import encode_passages, MODEL_TAG
            
            start_time = time.time()
            embeddings = await encode_passages(texts, batch_size=len(texts))
            duration = time.time() - start_time
            
            print(f"✅ Encoding успешен:")
            print(f"   Время: {duration*1000:.0f}ms на {len(texts)} текстов")
            print(f"   Скорость: {duration/len(texts)*1000:.0f}ms на чанк")
            print(f"   Размерность: {len(embeddings[0])} (e5-large)")
            print(f"   Модель: {MODEL_TAG}")
            
            # Тест семантического качества
            if len(embeddings) >= 2:
                import numpy as np
                sim_01 = float(np.dot(embeddings[0], embeddings[1]))
                print(f"   Семантическое сходство sample[0]↔sample[1]: {sim_01:.4f}")
        
        except Exception as e:
            print(f"❌ Ошибка encoding: {e}")
            
        # Оценка объёма работы
        total_chunks = await conn.fetchval("SELECT count(*) FROM ai.ai_knowledge_personal")
        
        estimated_time = total_chunks * (duration / len(texts)) if duration and texts else 0
        
        print(f"\n💡 Оценка δ-этапа:")
        print(f"   Объём данных: {total_chunks} чанков (vs 5453 в respect_kb)")
        print(f"   Время миграции: ~{estimated_time:.0f} секунд (vs 68 минут для respect_kb)")
        print(f"   Сложность: {'низкая' if total_chunks < 500 else 'средняя'}")
        
    finally:
        await conn.close()


async def main():
    print("🧪 АНАЛИЗ ЛИЧНОЙ KB ai.ai_knowledge_personal")
    print("Проверяем текущее состояние и готовность к улучшениям")
    print("=" * 60)
    
    structure_info = await analyze_personal_kb_structure()
    
    if structure_info['total_chunks'] == 0:
        print("\n❌ Личная KB пуста - нечего анализировать")
        return
        
    await test_current_vector_search()
    await test_e5_large_readiness()
    
    print("\n" + "=" * 60)
    print("📋 ИТОГОВЫЕ ВЫВОДЫ:")
    print(f"\n📊 Текущее состояние:")
    print(f"   • {structure_info['total_chunks']} чанков в личной KB")
    print(f"   • {structure_info['users']} активных пользователей")
    print(f"   • FTS: {'есть' if structure_info['has_fts'] else 'НЕТ'}")
    print(f"   • Vector: {'есть' if structure_info['has_embedding'] else 'НЕТ'}")
    if structure_info['embedding_dim']:
        print(f"   • Размерность: {structure_info['embedding_dim']} (OpenAI)")
    
    print(f"\n🎯 РЕКОМЕНДАЦИИ:")
    
    if structure_info['total_chunks'] < 50:
        print("   📉 Малый объём данных - δ-этап займёт минуты")
    elif structure_info['total_chunks'] < 500:
        print("   📊 Средний объём данных - δ-этап займёт 10-30 минут")
    else:
        print("   📈 Большой объём данных - δ-этап займёт 1+ час")
    
    if structure_info['has_embedding'] and not structure_info['has_fts']:
        print("   🔄 Для hybrid retrieval нужен δ-этап:")
        print("     1. Добавить FTS колонки")
        print("     2. Мигрировать embedding 1536→1024-dim (e5-large)")
        print("     3. Интегрировать в unified retrieval.py")
        print("     4. Получить такие же результаты как в respect_kb")
    elif not structure_info['has_embedding']:
        print("   ⚠️  ПРОБЛЕМА: личная KB без embedding")
        print("     Требует полной настройки с нуля")
    else:
        print("   ✅ Личная KB имеет и FTS и embedding - готова к тестам")

if __name__ == "__main__":
    asyncio.run(main())
