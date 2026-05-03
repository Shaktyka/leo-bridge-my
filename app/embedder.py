"""
v1.9.0: Unified embedding service для всех KB Leo.

Singleton-обёртка над sentence-transformers с multilingual-e5-large.
Lazy load при первом обращении, модель держится в памяти процесса.
Async через executor чтобы не блокировать event loop.

Модель: intfloat/multilingual-e5-large (Microsoft)
- 1024-dim vectors, normalized
- Обучена с префиксами: "passage: <text>" для документов, "query: <text>" для запросов
- Специально оптимизирована для multilingual semantic search

Используется в:
- respect_kb_search.py (корпоративная KB)
- internal_api.py (старая корпоративная + личная KB)
- respect_kb_sync.py (embedding новых карточек при sync)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List

from sentence_transformers import SentenceTransformer

log = logging.getLogger("embedder")

# Singleton state
_model: SentenceTransformer | None = None
_lock = asyncio.Lock()

# Constants
MODEL_NAME = "intfloat/multilingual-e5-large"
MODEL_TAG = "e5-large-v1"  # для записи в БД embedding_model колонку
EMBEDDING_DIM = 1024


async def get_model() -> SentenceTransformer:
    """
    Получить модель (singleton, lazy load).
    Первый вызов загружает ~2.2GB в память, дальше мгновенно.
    """
    global _model
    if _model is None:
        async with _lock:
            if _model is None:
                log.info(f"Loading embedding model {MODEL_NAME}...")
                loop = asyncio.get_running_loop()
                # Блокирующая загрузка в executor
                _model = await loop.run_in_executor(
                    None, SentenceTransformer, MODEL_NAME
                )
                log.info(f"Embedding model loaded: {EMBEDDING_DIM}-dim")
    return _model


async def encode_passages(
    texts: List[str], 
    batch_size: int = 16
) -> List[List[float]]:
    """
    Кодирование текстов документов с префиксом passage:.
    
    Args:
        texts: список текстов для индексации
        batch_size: размер батча для модели
        
    Returns:
        список векторов (каждый 1024 float)
    """
    if not texts:
        return []
        
    model = await get_model()
    # Префикс обязателен для e5-модели
    prefixed = [f"passage: {text}" for text in texts]
    
    loop = asyncio.get_running_loop()
    embeddings = await loop.run_in_executor(
        None,
        lambda: model.encode(
            prefixed,
            normalize_embeddings=True,
            batch_size=batch_size,
            show_progress_bar=False,
        ),
    )
    return embeddings.tolist()


async def encode_query(text: str) -> List[float]:
    """
    Кодирование поискового запроса с префиксом query:.
    
    Args:
        text: текст запроса
        
    Returns:
        вектор (1024 float)
    """
    model = await get_model()
    # Префикс обязателен для e5-модели  
    prefixed = f"query: {text}"
    
    loop = asyncio.get_running_loop()
    embedding = await loop.run_in_executor(
        None,
        lambda: model.encode(prefixed, normalize_embeddings=True),
    )
    return embedding.tolist()


def vector_literal(embedding: List[float]) -> str:
    """
    Форматирование embedding для pgvector cast в SQL.
    
    Args:
        embedding: список float от encode_passages/encode_query
        
    Returns:
        строка вида [0.1234567,-0.2345678,...] для использования в
        SQL как $1::vector
    """
    return "[" + ",".join(f"{x:.7f}" for x in embedding) + "]"


def get_model_info() -> dict[str, Any]:
    """Информация о модели для аудита."""
    return {
        "model_name": MODEL_NAME,
        "model_tag": MODEL_TAG,
        "embedding_dim": EMBEDDING_DIM,
        "loaded": _model is not None,
    }
