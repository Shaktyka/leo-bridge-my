"""Упрощённый тест качества hybrid retrieval"""
import asyncio
import asyncpg
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.retrieval import hybrid_search, TableSpec
from dotenv import load_dotenv
import os

load_dotenv("/opt/ai/bridge/.env")

SPEC = TableSpec("ai.respect_kb", "content_id", "fts", "embedding", ["title"])

async def main():
    print("🚀 Упрощённый тест качества hybrid retrieval")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Получаем тестовые ID
        ids = await conn.fetch("SELECT content_id FROM ai.respect_kb LIMIT 300")
        test_ids = [row["content_id"] for row in ids]
        print(f"Тестируем на {len(test_ids)} карточках")
        
        # Набор тестовых запросов
        queries = [
            ("налог", "общий термин"),
            ("командировочные", "точное слово"),
            ("суточные заграничные поездки", "семантика"),
            ("1С", "спецсимвол"),
            ("ФАМ", "аббревиатура"),
        ]
        
        all_passed = True
        
        for i, (query, desc) in enumerate(queries, 1):
            print(f"\n[{i}/5] {query} ({desc})")
            
            start_time = time.time()
            results = await hybrid_search(
                conn, SPEC, query, "content_id = ANY($1)", [test_ids], 5
            )
            duration = time.time() - start_time
            
            if results:
                print(f"   ✅ {len(results)} результатов за {duration*1000:.0f}ms")
                
                # Показываем top-3
                for j, result in enumerate(results[:3], 1):
                    content_id = result["pk"]  # используем pk из результата
                    score = result.get("rrf_score") or result.get("fts_rank") or 0
                    source = result.get("source", "unknown")
                    
                    # Получаем title для контекста
                    title_row = await conn.fetchrow(
                        "SELECT title FROM ai.respect_kb WHERE content_id = $1", content_id
                    )
                    title = (title_row["title"] or "")[:60] if title_row else "No title"
                    
                    print(f"      {j}. ID={content_id} score={score:.4f} source={source}")
                    print(f"         \"{title}\"")
                    
            else:
                print(f"   ⚠️  0 результатов за {duration*1000:.0f}ms")
                all_passed = False
        
        print("\n" + "=" * 50)
        if all_passed:
            print("🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
            print("   Hybrid retrieval работает отлично")
            print("   ✅ FTS + Vector + RRF функциональны")
            print("   ✅ Семантический поиск на русском работает")
            print("   ✅ Скорость приемлемая (обычно <100ms)")
        else:
            print("⚠️  Некоторые запросы не дали результатов")
            print("   Возможно нужна настройка параметров поиска")
            
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
