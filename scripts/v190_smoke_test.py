"""
v1.9.0 α.9: Smoke test для hybrid retrieval.

8 тестовых запросов:
- Точные совпадения → FTS должен быть сильным  
- Семантика → vector должен быть сильным
- Гибридные → RRF должен объединить лучшее

Для каждого запроса получаем top-5 и анализируем источники.
"""
import asyncio
import asyncpg
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.retrieval import hybrid_search, TableSpec
from app.respect_db import RespectDBClient
from dotenv import load_dotenv
import os

load_dotenv("/opt/ai/bridge/.env")

RESPECT_KB_SPEC = TableSpec(
    name="ai.respect_kb",
    pk="content_id", 
    fts_column="fts",
    embedding_column="embedding",
    text_columns=["title", "indexable_text"],
)

# Тестовые запросы
TEST_QUERIES = [
    # FTS-friendly (точные совпадения, аббревиатуры)
    ("Гарант", "точное название"),
    ("сравнение с Гарантом", "точная фраза"),  
    ("ФАМ", "аббревиатура"),
    ("1С", "спецсимвол"),
    
    # Vector-friendly (семантика, синонимы)
    ("суточные при заграничных поездках", "семантика командировок"),
    ("увольнение работника", "семантика трудовых отношений"),
    
    # Hybrid-friendly (общие темы)
    ("примеры расчётов по налогам", "широкая тема"),
    
    # Sanity check
    ("xyzabcnonsense", "ничего не должно найтись"),
]

async def main():
    print("v1.9.0 α.9: Smoke test hybrid retrieval")
    print("=" * 50)
    
    # Подключения
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Получаем тестового пользователя с доступом к content
        respect_client = await RespectDBClient.from_env().__aenter__()
        try:
            test_user = "@slv:mtx.respectrb.ru"  # предполагаем что у него есть доступ
            accessible_ids = await respect_client.kb_get_accessible_content_ids(test_user)
            print(f"Test user {test_user} has access to {len(accessible_ids)} content_ids")
            
            if len(accessible_ids) < 100:
                print("WARNING: Test user has limited access, results may be sparse")
            
        except Exception as e:
            print(f"Failed to get ACL for test user: {e}")
            # Fallback: используем первые 1000 content_ids
            accessible_ids = await conn.fetch(
                "SELECT content_id FROM ai.respect_kb ORDER BY content_id LIMIT 1000"
            )
            accessible_ids = [row["content_id"] for row in accessible_ids]
            print(f"Fallback: using first {len(accessible_ids)} content_ids")
        
        finally:
            await respect_client.__aexit__(None, None, None)
        
        print()
        
        # Smoke tests
        for i, (query, description) in enumerate(TEST_QUERIES, 1):
            print(f"[{i}/{len(TEST_QUERIES)}] «{query}» ({description})")
            
            try:
                results = await hybrid_search(
                    conn=conn,
                    spec=RESPECT_KB_SPEC,
                    query=query,
                    access_filter_sql="content_id = ANY($1)",
                    access_filter_params=[accessible_ids],
                    limit=5,
                )
                
                if not results:
                    print("   → 0 results")
                else:
                    print(f"   → {len(results)} results:")
                    for j, row in enumerate(results, 1):
                        score = row.get("rrf_score", 0) or row.get("fts_rank", 0) or row.get("vec_sim", 0)
                        source = row.get("source", "unknown")
                        print(f"     {j}. content_id={row[pk]} score={score:.4f} source={source}")
                        
                        # Подгружаем title для контекста
                        title_row = await conn.fetchrow(
                            "SELECT title FROM ai.respect_kb WHERE content_id = $1", 
                            row[pk]
                        )
                        if title_row:
                            title = (title_row["title"] or "")[:80]
                            print(f"        \"{title}\"")
                
                print()
                
            except Exception as e:
                print(f"   → ERROR: {e}")
                print()
        
        print("=" * 50)
        print("✓ SMOKE TEST COMPLETED")
        print()
        print("АНАЛИЗ:")
        print("- Точные запросы (Гарант, ФАМ, 1С) должны иметь source=fts")  
        print("- Семантические (суточные, увольнение) должны иметь source=vector")
        print("- Гибридные темы могут быть любого source")
        print("- xyzabcnonsense должен вернуть 0 results")
        
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
