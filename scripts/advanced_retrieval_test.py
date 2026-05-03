"""
Продвинутое тестирование и настройка hybrid retrieval v1.9.0-alpha.

Анализируем:
1. FTS vs Vector balance - почему всё идёт через vector?
2. RRF параметры - оптимальный k
3. Качество ранжирования по релевантности
4. Performance профилирование
"""
import asyncio
import asyncpg
import sys
import time
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.retrieval import hybrid_search, TableSpec, _fts_search, _vector_search, _reciprocal_rank_fusion
from dotenv import load_dotenv

load_dotenv("/opt/ai/bridge/.env")

SPEC = TableSpec("ai.respect_kb", "content_id", "fts", "embedding", ["title", "indexable_text"])

async def test_fts_vs_vector_separately():
    """Тестируем FTS и Vector поиск по отдельности"""
    print("🔍 Тест 1: FTS vs Vector по отдельности")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        ids = await conn.fetch("SELECT content_id FROM ai.respect_kb LIMIT 500")
        test_ids = [row["content_id"] for row in ids]
        
        test_queries = [
            "командировочные расходы",
            "налог на прибыль", 
            "1С бухгалтерия",
            "суточные заграничные поездки"
        ]
        
        for query in test_queries:
            print(f"\nЗапрос: '{query}'")
            
            # FTS отдельно
            start = time.time()
            fts_results = await _fts_search(
                conn, SPEC, [query], "content_id = ANY($1)", [test_ids]
            )
            fts_time = time.time() - start
            
            # Vector отдельно  
            start = time.time()
            vec_results = await _vector_search(
                conn, SPEC, query, "content_id = ANY($1)", [test_ids]
            )
            vec_time = time.time() - start
            
            print(f"   FTS:    {len(fts_results)} results, {fts_time*1000:.0f}ms")
            print(f"   Vector: {len(vec_results)} results, {vec_time*1000:.0f}ms")
            
            # Показываем топы для сравнения
            if fts_results:
                top_fts = fts_results[0]
                print(f"   FTS top:    ID={top_fts['pk']} rank={top_fts['fts_rank']:.4f}")
            
            if vec_results:
                top_vec = vec_results[0]  
                print(f"   Vector top: ID={top_vec['pk']} sim={top_vec['vec_sim']:.4f}")
            
            # Пересечения
            if fts_results and vec_results:
                fts_ids = {r['pk'] for r in fts_results[:10]}
                vec_ids = {r['pk'] for r in vec_results[:10]}
                overlap = len(fts_ids & vec_ids)
                print(f"   Пересечение top-10: {overlap}/10")
                
                # Анализ почему vector доминирует
                if len(vec_results) > len(fts_results):
                    print(f"   ⚠️  Vector нашёл больше ({len(vec_results)} vs {len(fts_results)})")
                elif overlap < 3:
                    print(f"   ⚠️  Слабое пересечение источников")
    
    finally:
        await conn.close()


async def test_rrf_parameters():
    """Тестируем разные параметры RRF"""
    print("\n⚙️  Тест 2: Оптимизация RRF параметров")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        ids = await conn.fetch("SELECT content_id FROM ai.respect_kb LIMIT 300")
        test_ids = [row["content_id"] for row in ids]
        
        query = "командировочные расходы"
        print(f"Тестовый запрос: '{query}'")
        
        # Получаем FTS и Vector результаты
        fts_results = await _fts_search(
            conn, SPEC, [query], "content_id = ANY($1)", [test_ids]
        )
        vec_results = await _vector_search(
            conn, SPEC, query, "content_id = ANY($1)", [test_ids]
        )
        
        print(f"FTS: {len(fts_results)} results, Vector: {len(vec_results)} results")
        
        if not fts_results or not vec_results:
            print("⚠️  Один из источников пустой - RRF неэффективен")
            return
        
        # Тестируем разные k для RRF
        k_values = [10, 30, 60, 100]
        
        for k in k_values:
            rrf_results = _reciprocal_rank_fusion(fts_results, vec_results, k=k)
            top_5 = rrf_results[:5]
            
            print(f"\n   RRF k={k}:")
            fts_count = 0
            vec_count = 0
            both_count = 0
            
            for i, r in enumerate(top_5, 1):
                score = r.get('rrf_score', 0)
                has_fts = 'fts_rank' in r
                has_vec = 'vec_sim' in r
                
                if has_fts and has_vec:
                    source = "both"
                    both_count += 1
                elif has_fts:
                    source = "fts"
                    fts_count += 1
                else:
                    source = "vector"
                    vec_count += 1
                
                print(f"     {i}. ID={r['pk']} score={score:.4f} source={source}")
            
            print(f"      Баланс: FTS={fts_count} Vector={vec_count} Both={both_count}")
    
    finally:
        await conn.close()


async def test_relevance_quality():
    """Ручная оценка релевантности результатов"""
    print("\n🎯 Тест 3: Анализ релевантности")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        ids = await conn.fetch("SELECT content_id FROM ai.respect_kb LIMIT 800")
        test_ids = [row["content_id"] for row in ids]
        
        # Запросы где мы можем оценить релевантность
        test_cases = [
            {
                "query": "командировочные расходы",
                "keywords": ["командировк", "расход", "суточн", "поездк", "служебн"]
            },
            {
                "query": "налог на прибыль", 
                "keywords": ["налог", "прибыл", "доход", "ставк", "расчет"]
            },
            {
                "query": "увольнение работника",
                "keywords": ["увольн", "работник", "трудов", "договор", "расторж"]
            }
        ]
        
        for case in test_cases:
            query = case["query"]
            keywords = case["keywords"]
            
            print(f"\nЗапрос: '{query}'")
            keywords_str = ", ".join(keywords)
            print(f"Ожидаемые слова: {keywords_str}")
            
            results = await hybrid_search(
                conn, SPEC, query, "content_id = ANY($1)", [test_ids], 7
            )
            
            if results:
                relevant_count = 0
                for i, r in enumerate(results, 1):
                    # Получаем полный текст для анализа
                    row = await conn.fetchrow("""
                        SELECT title, indexable_text 
                        FROM ai.respect_kb 
                        WHERE content_id = $1
                    """, r['pk'])
                    
                    if row:
                        full_text = f"{row['title']} {row['indexable_text']}".lower()
                        matches = sum(1 for kw in keywords if kw in full_text)
                        is_relevant = matches >= 1  # минимум 1 ключевое слово
                        
                        if is_relevant:
                            relevant_count += 1
                        
                        score = r.get('rrf_score', 0)
                        source = r.get('source', 'unknown')
                        status = '✅' if is_relevant else '❌'
                        
                        print(f"   {i}. ID={r['pk']} score={score:.4f} source={source} kw={matches}/{len(keywords)} {status}")
                        
                        title = (row['title'] or '')[:55] + "..." if len(row['title'] or '') > 55 else (row['title'] or 'No title')
                        print(f"      \"{title}\"")
                
                relevance_ratio = relevant_count / len(results)
                print(f"\n   Релевантность: {relevant_count}/{len(results)} ({relevance_ratio*100:.0f}%)")
                
                if relevance_ratio >= 0.8:
                    print("   🎉 Отличная релевантность!")
                elif relevance_ratio >= 0.6:
                    print("   ✅ Хорошая релевантность")
                elif relevance_ratio >= 0.4:
                    print("   ⚠️  Средняя релевантность")
                else:
                    print("   ❌ Низкая релевантность - требуется настройка")
            else:
                print("   ❌ Нет результатов!")
    
    finally:
        await conn.close()


async def test_performance_analysis():
    """Детальный анализ производительности"""
    print("\n⚡ Тест 4: Анализ производительности")
    print("=" * 50)
    
    conn = await asyncpg.connect(os.environ["DATABASE_URL_AI"])
    
    try:
        # Тест на разных размерах данных
        sizes = [100, 500, 1000, 2000]
        query = "налоговое планирование"
        
        print("Время отклика на разных объёмах данных:")
        
        for size in sizes:
            ids = await conn.fetch(f"SELECT content_id FROM ai.respect_kb LIMIT {size}")
            test_ids = [row["content_id"] for row in ids]
            
            # Замеряем компоненты отдельно
            start = time.time()
            fts_results = await _fts_search(
                conn, SPEC, [query], "content_id = ANY($1)", [test_ids]
            )
            fts_time = time.time() - start
            
            start = time.time()
            vec_results = await _vector_search(
                conn, SPEC, query, "content_id = ANY($1)", [test_ids]
            )
            vec_time = time.time() - start
            
            start = time.time()
            results = await hybrid_search(
                conn, SPEC, query, "content_id = ANY($1)", [test_ids], 5
            )
            total_time = time.time() - start
            
            print(f"   {size:4d} карточек: FTS={fts_time*1000:3.0f}ms Vector={vec_time*1000:3.0f}ms Total={total_time*1000:3.0f}ms")
        
        print("\nАнализ bottleneck'ов:")
        if vec_time > fts_time * 2:
            print("   ⚠️  Vector search медленнее FTS в 2+ раз")
            print("   💡 Рекомендация: оптимизировать IVFFlat параметры")
        
        if total_time > 0.2:  # 200ms
            print("   ⚠️  Общее время >200ms")
            print("   💡 Рекомендация: уменьшить top-k или добавить кэш")
        else:
            print("   ✅ Производительность в норме")
    
    finally:
        await conn.close()


async def main():
    print("🔧 ПРОДВИНУТОЕ ТЕСТИРОВАНИЕ И НАСТРОЙКА")
    print("Анализ hybrid retrieval v1.9.0-alpha для оптимизации качества")
    print("=" * 60)
    
    await test_fts_vs_vector_separately()
    await test_rrf_parameters()  
    await test_relevance_quality()
    await test_performance_analysis()
    
    print("\n" + "=" * 60)
    print("📊 ИТОГОВЫЕ РЕКОМЕНДАЦИИ:")
    print()
    print("🔧 Если FTS слабый:")
    print("   • Проверить text search config (russian vs simple)")
    print("   • Добавить синонимы в словарь") 
    print("   • Настроить весовые коэффициенты title/text")
    print()
    print("⚖️  Если Vector слишком доминирует:")
    print("   • Увеличить RRF k параметр (60→100)")
    print("   • Настроить FTS веса")
    print("   • Проверить качество embedding'ов")
    print()
    print("🎯 Если релевантность <70%:")
    print("   • Запустить β-этап (Cohere reranker)")
    print("   • Настроить query rewriting (γ-этап)")
    print("   • Добавить domain-specific filtering")
    print()
    print("⚡ Если время >200ms:")
    print("   • Уменьшить FTS_TOP_K/VEC_TOP_K")
    print("   • Оптимизировать IVFFlat индекс")
    print("   • Добавить результатный кэш")

if __name__ == "__main__":
    asyncio.run(main())
