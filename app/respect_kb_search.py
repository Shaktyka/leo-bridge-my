"""
v1.9.0: Поиск в синхронизированной КБ Респект.Чата.

ОБНОВЛЕНО в α.7: гибридный поиск FTS + vectors + RRF через app/retrieval.py
вместо legacy FTS-only.

Поток:
1. Получаем список доступных content_id для пользователя
2. Делаем hybrid_search() по ai.respect_kb с фильтром по этим id
3. Возвращаем результаты с ranking, snippets, attachments, cover_image_url

ВАЖНО: ACL применяется на стороне Респект.Чата — Leo не знает структуры прав.
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.respect_db import RespectDBClient
from app.retrieval import TableSpec, hybrid_search

log = logging.getLogger("respect_kb_search")

# TableSpec для respect_kb
RESPECT_KB_SPEC = TableSpec(
    name="ai.respect_kb",
    pk="content_id", 
    fts_column="fts",
    embedding_column="embedding",
    text_columns=["title", "indexable_text"],
)


async def respect_kb_search(
    *,
    leo_pool: asyncpg.Pool,
    respect_client: RespectDBClient,
    query: str,
    matrix_user_id: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Поиск в КБ Респект.Чата с учётом прав пользователя."""
    log.info(f"respect_kb_search: query='{query}' user={matrix_user_id} limit={limit}")
    
    # 1. Получаем список доступных content_id через ACL
    try:
        accessible_ids = await respect_client.get_accessible_content_ids(matrix_user_id)
        log.debug(f"User {matrix_user_id} has access to {len(accessible_ids)} content_ids")
        
        if not accessible_ids:
            # Нет прав — возвращаем пустой результат
            log.info("No accessible content for user, returning empty")
            return {
                "count": 0,
                "results": [],
                "query": query,
                "formatted_for_llm": f"По запросу «{query}» ничего не найдено в доступной базе знаний.",
            }
    except Exception as e:
        log.error(f"Failed to get accessible content_ids: {e}")
        raise
    
    # 2. Hybrid search через новый retrieval слой
    async with leo_pool.acquire() as conn:
        try:
            search_results = await hybrid_search(
                conn=conn,
                spec=RESPECT_KB_SPEC,
                query=query,
                access_filter_sql="content_id = ANY($1)",
                access_filter_params=[accessible_ids],
                limit=limit,
                rerank_top_n=None,  # β-этап: будет limit когда добавим reranker
            )
            
            log.debug(f"hybrid_search returned {len(search_results)} results")
            
            if not search_results:
                return {
                    "count": 0, 
                    "results": [],
                    "query": query,
                    "formatted_for_llm": f"По запросу «{query}» ничего не найдено в базе знаний.",
                }
            
        except Exception as e:
            log.error(f"hybrid_search failed: {e}")
            raise
    
    # 3. Обогащаем результаты
    content_ids = [row["pk"] for row in search_results]
    
    async with leo_pool.acquire() as conn:
        try:
            enriched_results = await _enrich_results(conn, query, content_ids, search_results)
        except Exception as e:
            log.error(f"Failed to enrich results: {e}")
            raise
    
    # 4. Форматируем для LLM
    formatted_text = _format_for_llm(query, enriched_results)
    
    return {
        "count": len(enriched_results),
        "results": enriched_results,
        "query": query, 
        "formatted_for_llm": formatted_text,
    }


async def _enrich_results(
    conn: asyncpg.Connection,
    query: str, 
    content_ids: list[int],
    search_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Подгружаем полные данные карточек + snippets + attachments."""
    
    # Подгружаем основные данные карточек
    main_sql = """
        SELECT 
            content_id,
            title,
            indexable_text,
            cover_image_url,
            ts_headline('russian', indexable_text, to_tsquery('russian', $1), 
                       'MaxWords=15, MinWords=10') AS snippet
        FROM ai.respect_kb 
        WHERE content_id = ANY($2)
    """
    
    card_rows = await conn.fetch(main_sql, query, content_ids)
    
    # Подгружаем attachments
    attach_sql = """
        SELECT 
            rca.content_id,
            ra.filename,
            ra.web_url
        FROM ai.respect_kb_content_attachments rca
        JOIN ai.respect_kb_attachments ra ON rca.attachment_id = ra.attachment_id
        WHERE rca.content_id = ANY($1)
        ORDER BY rca.content_id, ra.filename
    """
    
    attach_rows = await conn.fetch(attach_sql, content_ids)
    
    # Группируем attachments по content_id
    attachments_by_id = {}
    for row in attach_rows:
        content_id = row["content_id"]
        if content_id not in attachments_by_id:
            attachments_by_id[content_id] = []
        attachments_by_id[content_id].append({
            "name": row["filename"],
            "url": row["web_url"],
        })
    
    # Объединяем всё + preserve порядок от search_results
    search_by_id = {row["pk"]: row for row in search_results}
    cards_by_id = {row["content_id"]: row for row in card_rows}
    
    enriched = []
    for content_id in content_ids:
        search_data = search_by_id.get(content_id)
        card_data = cards_by_id.get(content_id)
        
        if not card_data:
            log.warning(f"Missing card data for content_id={content_id}")
            continue
            
        # Определяем score и source от search_results
        score = search_data.get("rrf_score") or search_data.get("fts_rank") or 0.0
        source = search_data.get("source", "unknown")
        
        enriched.append({
            "content_id": content_id,
            "title": card_data["title"] or "",
            "snippet": card_data["snippet"] or "",
            "score": float(score),
            "cover_image_url": card_data["cover_image_url"],
            "attachments": attachments_by_id.get(content_id, []),
            "source": source,
        })
    
    return enriched


def _format_for_llm(query: str, results: list[dict]) -> str:
    """Форматирование результатов для LLM."""
    if not results:
        return f"По запросу «{query}» ничего не найдено в базе знаний."
    
    lines = [f"Найдено {len(results)} результатов по запросу «{query}»:\n"]
    
    for i, result in enumerate(results, 1):
        title = result["title"]
        snippet = result["snippet"]
        score = result["score"]
        source = result["source"]
        attachments = result["attachments"]
        
        lines.append(f"{i}. {title}")
        lines.append(f"   {snippet}")
        lines.append(f"   [score: {score:.3f}, source: {source}]")
        
        if attachments:
            attach_names = [att["name"] for att in attachments[:3]]
            lines.append(f"   Файлы: {', '.join(attach_names)}")
            if len(attachments) > 3:
                lines.append(f"   ... и ещё {len(attachments)-3}")
        
        lines.append("")
    
    return "\n".join(lines)
