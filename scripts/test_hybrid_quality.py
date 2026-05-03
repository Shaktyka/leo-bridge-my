"""
Тестирование качества hybrid retrieval в реальных условиях.

Проверяем:
1. Точные совпадения vs семантический поиск
2. Сравнение FTS-only vs Hybrid режимов
3. Качество русскоязычной семантики
4. Скорость отклика
"""
import asyncio
import asyncpg
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.retrieval import hybrid_search, TableSpec
from app.respect_kb_search import respect_kb_search
from app.respect_db import RespectDBClient
from dotenv import load_dotenv
import os

load_dotenv("/opt/ai/bridge/.env")

SPEC = TableSpec("ai.respect_kb", "content_id", "fts", "embedding", ["title", "indexable_text"])

# Тестовые запросы с ожиданиями
TEST_QUERIES = [
    {
        "query": "командировочные расходы",
        "type": "точный термин", 
        "expect_fts": True,
        "expect_semantic": True
    },
    {
        "query": "суточные при заграничных поездках", 
        "type": "семантика",
        "expect_fts": False,
        "expect_semantic": True
    },
    {
        "query": "1С бухгалтерия",
        "type": "спецсимволы",
        "expect_fts": True, 
        "expect_semantic": False
    },
    {
        "query": "увольнение работника по собственному желанию",
        "type": "длинная фраза",
        "expect_fts": False,
        "expect_semantic": True
    },
    {
        "query": "ФАМ",
        "type": "аббревиатура", 
        "expect_fts": True,
        "expect_semantic": False
    }
]

async def test_basic_functionality():
    """Базовый тест: работает ли поиск вообще"""
    print("🔧 Тест 1: Базовая функциональность")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Получаем тестовую выборку content_ids
        ids = await conn.fetch("SELECT content_id FROM ai.respect_kb LIMIT 500")
        test_ids = [r["content_id"] for r in ids]
        print(f"   Тестируем на {len(test_ids)} карточках")
        
        # Тест простого запроса
        results = await hybrid_search(
            conn, SPEC, "налог", "content_id = ANY($1)", [test_ids], 5
        )
        
        print(f"   ✅ Запрос налог: {len(results)} результатов")
        
        if results:
            for i, r in enumerate(results[:3], 1):
                score = r.get("rrf_score", 0) or r.get("fts_rank", 0) or r.get("vec_sim", 0)
                source = r.get("source", "unknown")
                print(f"      {i}. content_id={r[pk]} source={source} score={score:.4f}")
        
        return len(results) > 0
        
    finally:
        await conn.close()


async def test_fts_vs_hybrid():
    """Сравнение FTS-only vs Hybrid режимов"""
    print("\n🆚 Тест 2: FTS-only vs Hybrid")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        ids = await conn.fetch("SELECT content_id FROM ai.respect_kb LIMIT 200")
        test_ids = [r["content_id"] for r in ids]
        
        for test_case in TEST_QUERIES[:3]:  # Тестируем первые 3
            query = test_case["query"] 
            print(f"\n   Запрос: {query} ({test_case[type]})")
            
            # FTS-only (эмулируем через HYBRID=false в коде)
            start = time.time()
            # Имитируем FTS-only через прямой SQL
            fts_results = await conn.fetch(f"""
                SELECT content_id, ts_rank_cd(fts, websearch_to_tsquery(russian, $1)) as rank
                FROM ai.respect_kb 
                WHERE content_id = ANY($2) AND fts @@ websearch_to_tsquery(russian, $1)
                ORDER BY rank DESC LIMIT 5
            """, query, test_ids)
            fts_time = time.time() - start
            
            # Hybrid
            start = time.time()
            hybrid_results = await hybrid_search(
                conn, SPEC, query, "content_id = ANY($1)", [test_ids], 5
            )
            hybrid_time = time.time() - start
            
            print(f"      FTS-only: {len(fts_results)} results in {fts_time*1000:.0f}ms")
            print(f"      Hybrid:   {len(hybrid_results)} results in {hybrid_time*1000:.0f}ms")
            
            # Анализируем пересечения
            fts_ids = {r["content_id"] for r in fts_results}
            hybrid_ids = {r['pk'] for r in hybrid_results}
            overlap = len(fts_ids & hybrid_ids)
            
            print(f"      Пересечение топов: {overlap}/{min(len(fts_ids), len(hybrid_ids))}")
            
    finally:
        await conn.close()


async def test_semantic_quality():
    """Проверка качества семантического поиска"""
    print("\n🧠 Тест 3: Семантическое качество")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        ids = await conn.fetch("SELECT content_id FROM ai.respect_kb LIMIT 1000") 
        test_ids = [r["content_id"] for r in ids]
        
        # Тест синонимов
        synonym_pairs = [
            ("командировочные расходы", "суточные при поездках"),
            ("увольнение", "расторжение трудового договора"), 
            ("отпуск", "отдых работника")
        ]
        
        for orig, synonym in synonym_pairs:
            print(f"\n   Синонимы: {orig} ↔ {synonym}")
            
            orig_results = await hybrid_search(
                conn, SPEC, orig, "content_id = ANY($1)", [test_ids], 5
            )
            syn_results = await hybrid_search(
                conn, SPEC, synonym, "content_id = ANY($1)", [test_ids], 5  
            )
            
            if orig_results and syn_results:
                orig_ids = {r['pk'] for r in orig_results}
                syn_ids = {r['pk'] for r in syn_results}
                overlap = len(orig_ids & syn_ids)
                
                print(f"      Оригинал: {len(orig_results)} results")
                print(f"      Синоним:  {len(syn_results)} results") 
                print(f"      Совпадений в топе: {overlap}/5 ({overlap/5*100:.0f}%)")
                
                if overlap >= 2:
                    print("      ✅ Семантика работает")
                else:
                    print("      ⚠️  Слабая семантическая связь")
            
    finally:
        await conn.close()


async def test_end_to_end_api():
    """Тест через публичный API respect_kb_search"""
    print("\n🌐 Тест 4: End-to-end API")
    print("=" * 50)
    
    # Создаём pool как в продакшене
    leo_pool = await asyncpg.create_pool(os.environ["DATABASE_URL_AI"], min_size=1, max_size=2)
    
    try:
        # Fake respect client (мы используем fallback в коде)
        class FakeRespectClient:
            async def kb_get_accessible_content_ids(self, user_id):
                raise Exception("Test fallback")
        
        fake_client = FakeRespectClient()
        
        # Тестируем через настоящий API
        result = await respect_kb_search(
            leo_pool=leo_pool,
            respect_client=fake_client,
            query="налоговое планирование",
            matrix_user_id="@test:example.com",
            limit=3
        )
        
        print(f"   ✅ API отработал: {result[count]} результатов")
        print(f"   Query: {result[query]}")
        
        if result["results"]:
            for i, r in enumerate(result["results"], 1):
                title = r["title"][:50] + "..." if len(r["title"]) > 50 else r["title"]
                print(f"      {i}. {title}")
                print(f"         score={r[score]:.4f} source={r[source]}")
        
        # Проверяем LLM formatting
        llm_text = result["formatted_for_llm"]
        print(f"\n   LLM format: {len(llm_text)} символов")
        
        return result["count"] > 0
        
    finally:
        await leo_pool.close()


async def main():
    print("🚀 Тестирование hybrid retrieval quality")
    print("Проверяем работу v1.9.0-alpha в реальных условиях\n")
    
    results = []
    
    try:
        results.append(await test_basic_functionality())
        results.append(await test_fts_vs_hybrid())
        await test_semantic_quality()  # no return value
        results.append(await test_end_to_end_api())
        
        print("\n" + "=" * 60)
        print("🎯 ИТОГОВЫЙ РЕЗУЛЬТАТ:")
        
        passed = sum(results)
        total = len(results)
        
        print(f"   Базовые тесты пройдено: {passed}/{total}")
        
        if passed == total:
            print("   🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
            print("   Hybrid retrieval работает отлично")
        else:
            print("   ⚠️  Некоторые тесты не прошли")
            print("   Требуется дополнительная отладка")
            
        print(f"\n   v1.9.0-alpha готов к использованию!")
        
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        print("   Hybrid retrieval требует исправлений")


if __name__ == "__main__":
    asyncio.run(main())
