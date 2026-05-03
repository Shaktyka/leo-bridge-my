"""
v1.9.0: Unified hybrid retrieval для всех KB Leo.

Общий pipeline для ai.respect_kb, ai.ai_knowledge, ai.ai_knowledge_personal:
1. Query rewriting (γ-этап) — пока pass-through
2. FTS top-50 + Vector top-50 параллельно  
3. Reciprocal Rank Fusion
4. Cohere reranker top-5 (β-этап) — пока pass-through
5. Подсветка ts_headline для FTS-совпавших

Каждая KB вызывает hybrid_search() со своим TableSpec и access_filter.
"""
from __future__ import annotations

import asyncio
import time
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg

from app.embedder import encode_query, vector_literal
from app.query_rewriter import rewrite_query, QUERY_REWRITE
from app.metrics import (
    SEARCH_TOTAL, SEARCH_DURATION, SEARCH_RESULTS_RETURNED,
    FTS_RESULTS, VECTOR_RESULTS, RRF_TOP_SOURCE,
    COHERE_CALLS, COHERE_DURATION,
)

log = logging.getLogger("retrieval")

# Конфигурация из env
FTS_TOP_K = int(os.environ.get("LEO_FTS_TOP_K", "50"))
VEC_TOP_K = int(os.environ.get("LEO_VEC_TOP_K", "50"))
RRF_K = int(os.environ.get("LEO_RRF_K", "60"))
# β-этап: Cohere reranker configuration
COHERE_RERANK = os.environ.get("COHERE_RERANK", "true").lower() == "true"
COHERE_MODEL = "rerank-multilingual-v3.0"
COHERE_TOP_K = 20  # кандидатов для rerank

# Инициализируем Cohere client
try:
    import cohere
    COHERE_CLIENT = cohere.Client(os.environ.get("COHERE_API_KEY")) if os.environ.get("COHERE_API_KEY") else None
except ImportError:
    COHERE_CLIENT = None
    log.warning("cohere library not installed")


# Feature flags (default true для нового поведения)
HYBRID = os.environ.get("RESPECT_KB_HYBRID", "true").lower() == "true"
RERANK = os.environ.get("RESPECT_KB_RERANK", "true").lower() == "true"
QUERY_REWRITE = os.environ.get("RESPECT_KB_QUERY_REWRITE", "true").lower() == "true"


@dataclass
class TableSpec:
    """Спецификация таблицы для unified retrieval."""
    name: str                    # "ai.respect_kb"
    pk: str                      # "content_id"
    fts_column: str             # "fts"
    embedding_column: str       # "embedding"  
    text_columns: List[str]     # ["title", "indexable_text"] для rerank




async def _cohere_rerank(
    conn: asyncpg.Connection,
    spec: TableSpec,
    query: str,
    candidates: List[Dict[str, Any]],
    top_k: int = 5
) -> List[Dict[str, Any]]:
    """
    Cohere rerank для финальной полировки результатов.
    
    Args:
        conn: Database connection
        spec: TableSpec для получения текстов
        query: Исходный запрос
        candidates: Кандидаты после RRF
        top_k: Количество финальных результатов
    
    Returns:
        Переранжированные результаты с cohere_score
    """
    if not COHERE_CLIENT:
        log.warning("Cohere API key not configured - skipping rerank")
        try:
            COHERE_CALLS.labels(result="skipped_no_key").inc()
        except Exception:
            pass
        return candidates[:top_k]
    
    if len(candidates) <= top_k:
        # Добавляем cohere_score = RRF score для совместимости
        for c in candidates:
            c["cohere_score"] = c.get("rrf_score", 0)
        try:
            COHERE_CALLS.labels(result="skipped_few_results").inc()
        except Exception:
            pass
        return candidates
    
    try:
        start_time = time.time()
        
        # Получаем полные тексты для кандидатов
        candidate_ids = [c["pk"] for c in candidates]
        
        # Формируем SQL для получения текстов
        text_columns = ", ".join(spec.text_columns) if spec.text_columns else "content"
        
        texts_sql = f"""
            SELECT {spec.pk}, {text_columns}
            FROM {spec.name}
            WHERE {spec.pk} = ANY($1::{spec.pk}[])
        """
        
        # Определяем тип массива для SQL по имени таблицы и PK
        if spec.pk == "id":  # UUID для personal KB
            texts_sql = texts_sql.replace(f"{spec.pk}[]", "uuid[]")
        elif spec.pk == "content_id":  # bigint для respect_kb
            texts_sql = texts_sql.replace(f"{spec.pk}[]", "bigint[]")
        else:  # text для остальных
            texts_sql = texts_sql.replace(f"{spec.pk}[]", "text[]")
        
        text_rows = await conn.fetch(texts_sql, candidate_ids)
        
        # Создаём мапинг pk -> text
        pk_to_text = {}
        for row in text_rows:
            # Объединяем все текстовые колонки
            text_parts = []
            for col in spec.text_columns or ["content"]:
                if col in row and row[col]:
                    text_parts.append(str(row[col]))
            
            full_text = " ".join(text_parts)
            pk_to_text[row[spec.pk]] = full_text[:8000]  # Ограничиваем размер
        
        # Подготавливаем документы для Cohere
        documents = []
        candidate_map = {}
        
        for i, candidate in enumerate(candidates):
            pk = candidate["pk"]
            text = pk_to_text.get(pk, "")
            
            if text:  # Только документы с текстом
                documents.append(text)
                candidate_map[i] = candidate
        
        if not documents:
            log.warning("No documents with text for Cohere rerank")
            return candidates[:top_k]
        
        # Вызываем Cohere rerank
        response = COHERE_CLIENT.rerank(
            query=query,
            documents=documents,
            model=COHERE_MODEL,
            top_n=min(top_k, len(documents)),
            return_documents=False
        )
        
        rerank_time = time.time() - start_time
        
        # Создаём результат
        reranked = []
        for result in response.results:
            original_candidate = candidate_map[result.index]
            
            # Добавляем Cohere score
            reranked_candidate = original_candidate.copy()
            reranked_candidate["cohere_score"] = float(result.relevance_score)
            reranked_candidate["cohere_rank"] = len(reranked) + 1
            
            reranked.append(reranked_candidate)
        
        log.debug(f"Cohere rerank: {len(candidates)}→{len(reranked)} in {rerank_time*1000:.0f}ms")
        
        # metrics
        try:
            COHERE_CALLS.labels(result="success").inc()
            COHERE_DURATION.observe(rerank_time)
        except Exception:
            pass
        
        return reranked
        
    except Exception as e:
        log.error(f"Cohere rerank failed: {e}")
        # metrics
        try:
            COHERE_CALLS.labels(result="error").inc()
        except Exception:
            pass
        # Fallback на RRF результаты
        return candidates[:top_k]


async def hybrid_search(
    conn: asyncpg.Connection,
    spec: TableSpec,
    query: str,
    access_filter_sql: str,
    access_filter_params: List[Any],
    limit: int = 50,
    rerank_top_n: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Универсальный гибридный поиск FTS + vectors + RRF.
    
    Args:
        conn: установленное соединение с БД
        spec: спецификация таблицы (колонки, pk)
        query: текст запроса
        access_filter_sql: WHERE clause для прав ("content_id = ANY($1)")
        access_filter_params: параметры для access filter
        limit: сколько итоговых результатов вернуть
        rerank_top_n: если задан — применить reranker к топу RRF
        
    Returns:
        список записей [{pk, fts_rank, vec_sim, rrf_score, ...}, ...]
        отсортированный по итоговому рангу
    """
    log.debug(f"hybrid_search: {spec.name}, query={query[:50]}..., limit={limit}")
    
    # === metrics (γ.4) ===
    import time as _time
    _t_start = _time.monotonic()
    SEARCH_TOTAL.labels(kb=spec.name).inc()
    
    if not HYBRID:
        # Fallback: только FTS (legacy behavior)
        return await _fts_only(conn, spec, query, access_filter_sql, access_filter_params, limit)
    
    # 1. Query rewriting (γ-этап) — расширяем запрос через Haiku
    # Возвращает [original, *rewrites] или [query] если flag выключен / запрос длинный / ошибка
    if QUERY_REWRITE:
        try:
            search_queries = await rewrite_query(query)
            if len(search_queries) > 1:
                log.debug(f"Query rewrites: {len(search_queries) - 1} variants")
        except Exception as e:
            log.warning(f"Query rewrite failed, using original: {e}")
            search_queries = [query]
    else:
        search_queries = [query]
    
    # 2. Параллельный поиск FTS + Vector
    fts_task = asyncio.create_task(_fts_search(
        conn, spec, search_queries, access_filter_sql, access_filter_params
    ))
    vec_task = asyncio.create_task(_vector_search(
        conn, spec, query, access_filter_sql, access_filter_params  
    ))
    
    fts_rows, vec_rows = await asyncio.gather(fts_task, vec_task)
    
    # 3. Reciprocal Rank Fusion
    rrf_results = _reciprocal_rank_fusion(fts_rows, vec_rows, k=RRF_K)
    
    # 4. Reranker (β-этап — пока пропускаем)
    if rerank_top_n is not None and RERANK:
        # TODO: β.2 — подгрузить text_columns и прогнать через Cohere
        log.debug("Reranker не реализован в α-этапе, используем RRF порядок")
    
    # β-этап: Cohere reranker для финальной полировки
    if COHERE_RERANK and len(rrf_results) > limit:
        # Берём больше кандидатов для rerank
        candidates = rrf_results[:COHERE_TOP_K]
        # metrics: учитываем источник ТОП РЕЗУЛЬТАТА RRF (до Cohere)
        if candidates:
            _top = candidates[0]
            _has_fts = "fts_rank" in _top
            _has_vec = "vec_sim" in _top
            if _has_fts and _has_vec:
                _src = "both"
            elif _has_fts:
                _src = "fts_only"
            else:
                _src = "vector_only"
            try:
                RRF_TOP_SOURCE.labels(kb=spec.name, source=_src).inc()
            except Exception:
                pass
        final_results = await _cohere_rerank(conn, spec, query, candidates, limit)
        # metrics
        SEARCH_DURATION.labels(kb=spec.name, stage="total").observe(_time.monotonic() - _t_start)
        SEARCH_RESULTS_RETURNED.labels(kb=spec.name).inc(len(final_results))
        return final_results
    else:
        # metrics
        results = rrf_results[:limit]
        SEARCH_DURATION.labels(kb=spec.name, stage="total").observe(_time.monotonic() - _t_start)
        SEARCH_RESULTS_RETURNED.labels(kb=spec.name).inc(len(results))
        # Учитываем источник топ-результата для RRF (без Cohere)
        if results:
            top = results[0]
            has_fts = "fts_rank" in top
            has_vec = "vec_sim" in top
            if has_fts and has_vec:
                src_label = "both"
            elif has_fts:
                src_label = "fts_only"
            else:
                src_label = "vector_only"
            RRF_TOP_SOURCE.labels(kb=spec.name, source=src_label).inc()
        return results


async def _fts_search(
    conn: asyncpg.Connection, 
    spec: TableSpec, 
    queries: List[str],
    access_filter_sql: str, 
    access_filter_params: List[Any],
) -> List[Dict[str, Any]]:
    """FTS поиск по всем вариантам query."""
    all_results = []
    
    for i, query in enumerate(queries):
        # $N+1 потому что params уже заняты access_filter
        param_idx = len(access_filter_params) + 1
        
        # Улучшенный FTS: более толерантный к multi-word запросам
        sql = f"""
            SELECT {spec.pk} AS pk,
                   ts_rank_cd({spec.fts_column}, 
                             plainto_tsquery('russian', ${param_idx})) AS fts_rank
            FROM {spec.name}
            WHERE {access_filter_sql}
              AND {spec.fts_column} @@ plainto_tsquery('russian', ${param_idx})
            ORDER BY fts_rank DESC 
            LIMIT {FTS_TOP_K}
        """
        
        rows = await conn.fetch(sql, *access_filter_params, query)
        
        # v1.9.0 FTS fix: fallback если plainto_tsquery не дал результатов
        if not rows and len(query.split()) > 1:
            # Пробуем простой поиск по отдельным словам
            words = [w for w in query.split() if len(w) > 2]  # игнорируем короткие слова
            if words:
                word_query = ' | '.join(words)  # OR логика
                fallback_sql = f"""
                    SELECT {spec.pk} AS pk,
                           ts_rank_cd({spec.fts_column}, 
                                     to_tsquery('russian', ${param_idx})) AS fts_rank
                    FROM {spec.name}
                    WHERE {access_filter_sql}
                      AND {spec.fts_column} @@ to_tsquery('russian', ${param_idx})
                    ORDER BY fts_rank DESC 
                    LIMIT {FTS_TOP_K}
                """
                try:
                    rows = await conn.fetch(fallback_sql, *access_filter_params, word_query)
                    log.debug(f"FTS fallback для '{query}': {len(rows)} results")
                except Exception as e:
                    log.warning(f"FTS fallback failed for '{query}': {e}")
        
        for row in rows:
            all_results.append({
                "pk": row["pk"],
                "fts_rank": float(row["fts_rank"]),
                "source": "fts",
                "query_variant": i,
            })
    
    # Dedupe и сортировка по лучшему rank для каждого pk
    pk_best = {}
    for row in all_results:
        pk = row["pk"]
        if pk not in pk_best or row["fts_rank"] > pk_best[pk]["fts_rank"]:
            pk_best[pk] = row
    
    _fts_results = list(pk_best.values())
    try:
        FTS_RESULTS.labels(kb=spec.name).observe(len(_fts_results))
    except Exception:
        pass
    return _fts_results


async def _vector_search(
    conn: asyncpg.Connection,
    spec: TableSpec, 
    query: str,
    access_filter_sql: str,
    access_filter_params: List[Any],
) -> List[Dict[str, Any]]:
    """Vector search через cosine similarity."""
    # Кодируем запрос
    query_embedding = await encode_query(query)
    query_literal = vector_literal(query_embedding)
    
    param_idx = len(access_filter_params) + 1
    
    sql = f"""
        SELECT {spec.pk} AS pk,
               1 - ({spec.embedding_column} <=> ${param_idx}::vector) AS vec_sim
        FROM {spec.name}
        WHERE {access_filter_sql}
          AND {spec.embedding_column} IS NOT NULL
        ORDER BY {spec.embedding_column} <=> ${param_idx}::vector
        LIMIT {VEC_TOP_K}
    """
    
    rows = await conn.fetch(sql, *access_filter_params, query_literal)
    _vec_results = [
        {
            "pk": row["pk"],
            "vec_sim": float(row["vec_sim"]),
            "source": "vector",
        }
        for row in rows
    ]
    try:
        VECTOR_RESULTS.labels(kb=spec.name).observe(len(_vec_results))
    except Exception:
        pass
    return _vec_results
def _reciprocal_rank_fusion(
    fts_results: List[Dict[str, Any]], 
    vec_results: List[Dict[str, Any]],
    k: int = 60,
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion: объединение FTS + vector rankings."""
    scores = {}
    
    # FTS contribution
    for rank, row in enumerate(fts_results, start=1):
        pk = row["pk"]
        rrf_score = 1.0 / (k + rank)
        scores[pk] = scores.get(pk, 0) + rrf_score
    
    # Vector contribution  
    for rank, row in enumerate(vec_results, start=1):
        pk = row["pk"]
        rrf_score = 1.0 / (k + rank)
        scores[pk] = scores.get(pk, 0) + rrf_score
    
    # Объединяем metadata от обоих sources
    pk_metadata = {}
    for row in fts_results:
        pk_metadata[row["pk"]] = {**pk_metadata.get(row["pk"], {}), **row}
    for row in vec_results:
        pk_metadata[row["pk"]] = {**pk_metadata.get(row["pk"], {}), **row}
    
    # Сортируем по итоговому RRF score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    return [
        {
            "pk": pk,
            "rrf_score": score,
            **pk_metadata.get(pk, {}),
        }
        for pk, score in ranked
    ]


async def _fts_only(
    conn: asyncpg.Connection,
    spec: TableSpec, 
    query: str,
    access_filter_sql: str,
    access_filter_params: List[Any],
    limit: int,
) -> List[Dict[str, Any]]:
    """Legacy FTS-only поиск когда HYBRID=false."""
    param_idx = len(access_filter_params) + 1
    
    sql = f"""
        SELECT {spec.pk} AS pk,
               ts_rank_cd({spec.fts_column}, 
                         plainto_tsquery('russian', ${param_idx})) AS fts_rank
        FROM {spec.name}
        WHERE {access_filter_sql}
          AND {spec.fts_column} @@ plainto_tsquery('russian', ${param_idx})
        ORDER BY fts_rank DESC 
        LIMIT {limit}
    """
    
    rows = await conn.fetch(sql, *access_filter_params, query)
    return [
        {
            "pk": row["pk"],
            "fts_rank": float(row["fts_rank"]),
            "source": "fts_only",
        }
        for row in rows
    ]
