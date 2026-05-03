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
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg

from app.embedder import encode_query, vector_literal

log = logging.getLogger("retrieval")

# Конфигурация из env
FTS_TOP_K = int(os.environ.get("LEO_FTS_TOP_K", "50"))
VEC_TOP_K = int(os.environ.get("LEO_VEC_TOP_K", "50"))
RRF_K = int(os.environ.get("LEO_RRF_K", "60"))

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
    
    if not HYBRID:
        # Fallback: только FTS (legacy behavior)
        return await _fts_only(conn, spec, query, access_filter_sql, access_filter_params, limit)
    
    # 1. Query rewriting (γ-этап — пока пропускаем)
    search_queries = [query]  # TODO: γ.2 — expand через query_rewriter
    
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
    
    return rrf_results[:limit]


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
        
        sql = f"""
            SELECT {spec.pk} AS pk,
                   ts_rank_cd({spec.fts_column}, 
                             websearch_to_tsquery('russian', ${param_idx})) AS fts_rank
            FROM {spec.name}
            WHERE {access_filter_sql}
              AND {spec.fts_column} @@ websearch_to_tsquery('russian', ${param_idx})
            ORDER BY fts_rank DESC 
            LIMIT {FTS_TOP_K}
        """
        
        rows = await conn.fetch(sql, *access_filter_params, query)
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
    
    return list(pk_best.values())


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
    return [
        {
            "pk": row["pk"],
            "vec_sim": float(row["vec_sim"]),
            "source": "vector",
        }
        for row in rows
    ]


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
                         websearch_to_tsquery('russian', ${param_idx})) AS fts_rank
        FROM {spec.name}
        WHERE {access_filter_sql}
          AND {spec.fts_column} @@ websearch_to_tsquery('russian', ${param_idx})
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
