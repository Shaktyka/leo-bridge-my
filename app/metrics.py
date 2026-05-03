"""
Prometheus метрики для Leo v1.9.0-β.

Все метрики используют default registry prometheus_client.
Экспонируются через FastAPI endpoint /metrics в internal_api.py.

Категории:
- leo_search_*       — общие метрики hybrid_search
- leo_fts_*          — FTS-слой
- leo_vector_*       — Vector search
- leo_rrf_*          — RRF результаты
- leo_cohere_*       — Cohere reranker
- leo_query_rewrite_* — γ-этап query rewriting
- leo_kb_*           — счётчики по KB

Пример promQL запросов:
  # Latency p95 hybrid_search по KB
  histogram_quantile(0.95, sum(rate(leo_search_duration_seconds_bucket[5m])) by (kb, le))
  
  # Rewrite cache hit ratio
  sum(rate(leo_query_rewrite_total{result="cache_hit"}[5m]))
    / sum(rate(leo_query_rewrite_total[5m]))
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge

# === Hybrid search overall ===

SEARCH_TOTAL = Counter(
    "leo_search_total",
    "Общее число hybrid_search вызовов",
    ["kb"],  # ai.respect_kb / ai.ai_knowledge_personal / ai.ai_knowledge
)

SEARCH_DURATION = Histogram(
    "leo_search_duration_seconds",
    "Latency hybrid_search по слоям",
    ["kb", "stage"],  # stage: total / fts / vector / rrf / cohere / rewrite
    buckets=(0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0),
)

SEARCH_RESULTS_RETURNED = Counter(
    "leo_search_results_returned_total",
    "Сколько результатов возвращено пользователю",
    ["kb"],
)

# === FTS layer ===

FTS_RESULTS = Histogram(
    "leo_fts_results",
    "Распределение количества результатов FTS",
    ["kb"],
    buckets=(0, 1, 5, 10, 25, 50, 100),
)

# === Vector search ===

VECTOR_RESULTS = Histogram(
    "leo_vector_results",
    "Распределение количества результатов Vector",
    ["kb"],
    buckets=(0, 1, 5, 10, 25, 50, 100),
)

# === RRF result composition ===
# В топ-N сколько было только-FTS, только-Vector, both?
RRF_TOP_SOURCE = Counter(
    "leo_rrf_top_source_total",
    "Источник топ-результата после RRF",
    ["kb", "source"],  # source: fts_only / vector_only / both
)

# === Cohere reranker ===

COHERE_CALLS = Counter(
    "leo_cohere_rerank_total",
    "Вызовы Cohere reranker",
    ["result"],  # success / error / skipped (нет API key) / skipped_few_results
)

COHERE_DURATION = Histogram(
    "leo_cohere_duration_seconds",
    "Latency Cohere rerank API",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

# === Query rewriting (γ-этап) ===

QUERY_REWRITE_CALLS = Counter(
    "leo_query_rewrite_total",
    "Вызовы query rewriter",
    ["result"],  # success / skip_long / skip_disabled / cache_hit / error
)

QUERY_REWRITE_DURATION = Histogram(
    "leo_query_rewrite_duration_seconds",
    "Latency Haiku rewrite (без кэша)",
    buckets=(0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0),
)

QUERY_REWRITE_VARIANTS = Histogram(
    "leo_query_rewrite_variants",
    "Сколько вариантов вернул Haiku (без оригинала)",
    buckets=(0, 1, 2, 3, 4, 5),
)

QUERY_REWRITE_CACHE_SIZE = Gauge(
    "leo_query_rewrite_cache_size",
    "Текущий размер in-memory cache rewrites",
)


def get_metrics_text() -> tuple[bytes, str]:
    """
    Возвращает (body_bytes, content_type) для prometheus exposition format.
    
    Используется в FastAPI/aiohttp handler:
        body, content_type = get_metrics_text()
        return Response(content=body, media_type=content_type)
    """
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
