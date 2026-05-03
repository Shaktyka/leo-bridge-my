"""
Query rewriter для γ-этапа hybrid retrieval.

Идея:
    Короткие/нечёткие запросы плохо работают через FTS:
        "как уволиться?" -> 0 результатов в FTS
    
    Через Haiku 4.5 генерируем 2-3 переформулировки и прогоняем их параллельно:
        ["как уволиться?",
         "увольнение по собственному желанию работника",
         "процедура расторжения трудового договора"]
    
    _fts_search принимает список вариантов и объединяет результаты через RRF.
    Vector search использует только оригинальный query (его embedding).

Принципы:
    - Опциональность через feature flag QUERY_REWRITE
    - Heuristic skip: rewrite только для коротких запросов (< 6 слов)
    - In-memory LRU cache на 1000 элементов
    - Graceful fallback: ошибка Haiku -> возврат [original_query]
    - Низкая latency (~200-400ms на rewrite, кэшируется)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

log = logging.getLogger("query_rewriter")

try:
    from app.metrics import (
        QUERY_REWRITE_CALLS, QUERY_REWRITE_DURATION,
        QUERY_REWRITE_VARIANTS, QUERY_REWRITE_CACHE_SIZE,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

# === Feature flag ===
QUERY_REWRITE = os.environ.get("QUERY_REWRITE", "true").lower() == "true"
HAIKU_MODEL = os.environ.get("HAIKU_MODEL", "claude-haiku-4-5-20251001")

# Heuristic: если запрос длинный — он уже описательный, rewrite не нужен
MIN_WORDS_FOR_REWRITE = 1   # включительно
MAX_WORDS_FOR_REWRITE = 5   # включительно (>=6 не трогаем)
MAX_REWRITES = 3

# Anthropic client (lazy init)
_anthropic_client = None


def _get_client():
    """Lazy init Anthropic client."""
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not configured — query rewriting disabled")
        return None
    
    try:
        from anthropic import AsyncAnthropic
        _anthropic_client = AsyncAnthropic(api_key=api_key)
        return _anthropic_client
    except ImportError:
        log.warning("anthropic library not installed — query rewriting disabled")
        return None


# === Простой in-memory cache ===
# Структура: query (str) -> list[str] (rewrites включая оригинал)
_cache: dict[str, list[str]] = {}
_cache_max_size = 1000


def _cache_get(query: str) -> Optional[list[str]]:
    return _cache.get(query)


def _cache_put(query: str, rewrites: list[str]):
    if len(_cache) >= _cache_max_size:
        # Простой FIFO eviction — удаляем первый ключ
        first_key = next(iter(_cache))
        del _cache[first_key]
    _cache[query] = rewrites


# === Системный промпт для Haiku ===
SYSTEM_PROMPT = """Ты помощник для расширения поисковых запросов в корпоративной базе знаний на русском языке.

Получая короткий запрос пользователя, верни 2-3 переформулировки которые помогут полнотекстовому поиску найти больше релевантных документов.

Правила:
- Используй разные синонимы и формулировки
- Включай профессиональную терминологию (например, "увольнение" -> "расторжение трудового договора")
- Не добавляй лишних подробностей которых не было в запросе
- Каждая переформулировка — на новой строке, без нумерации, без пояснений
- Только русский язык
- Максимум 3 переформулировки

Примеры:

Запрос: как уволиться?
Ответ:
увольнение по собственному желанию работника
расторжение трудового договора по инициативе сотрудника
оформление заявления об увольнении

Запрос: командировки
Ответ:
оформление командировочных расходов
служебная командировка работника
суточные при командировках

Запрос: налог на прибыль 2026
Ответ:
ставка налога на прибыль организаций 2026
расчёт налога на прибыль для юридических лиц
изменения по налогу на прибыль в 2026 году"""


async def _haiku_rewrite(query: str) -> list[str]:
    """
    Запрос к Haiku для получения переформулировок.
    Возвращает только rewrites (без оригинала).
    Если ошибка — возвращает пустой список.
    """
    client = _get_client()
    if client is None:
        return []
    
    try:
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            temperature=0.0,  # детерминизм для кэша
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Запрос: {query}\nОтвет:"}],
        )
        
        # Парсим текстовый ответ
        text = response.content[0].text.strip()
        
        # Каждая строка — одна переформулировка
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        
        # Убираем пустые и слишком короткие (защита от галлюцинаций)
        rewrites = [line for line in lines if len(line) >= 3]
        
        # Ограничение
        rewrites = rewrites[:MAX_REWRITES]
        
        log.debug(f"Haiku rewrites for '{query[:40]}': {len(rewrites)}")
        return rewrites
        
    except Exception as e:
        log.warning(f"Haiku rewrite failed for '{query[:50]}': {e}")
        return []


async def rewrite_query(query: str) -> list[str]:
    """
    Главная функция: возвращает список вариантов запроса для поиска.
    
    Возвращает [original_query, *rewrites] чтобы оригинал тоже шёл в поиск.
    
    Если rewriting отключён или запрос длинный — возвращает [query].
    Если Haiku упал — graceful fallback на [query].
    """
    if not query or not query.strip():
        return [query] if query else [""]
    
    query = query.strip()
    
    # Heuristic skip: длинные запросы уже описательные
    word_count = len(query.split())
    if word_count > MAX_WORDS_FOR_REWRITE:
        log.debug(f"Skip rewrite for long query ({word_count} words)")
        if _METRICS_AVAILABLE:
            QUERY_REWRITE_CALLS.labels(result="skip_long").inc()
        return [query]
    
    # Feature flag
    if not QUERY_REWRITE:
        if _METRICS_AVAILABLE:
            QUERY_REWRITE_CALLS.labels(result="skip_disabled").inc()
        return [query]
    
    # Cache check
    cached = _cache_get(query)
    if cached is not None:
        log.debug(f"Cache hit for '{query[:40]}'")
        if _METRICS_AVAILABLE:
            QUERY_REWRITE_CALLS.labels(result="cache_hit").inc()
        return cached
    
    # Haiku call
    import time as _time
    _t_start = _time.monotonic()
    rewrites = await _haiku_rewrite(query)
    _duration = _time.monotonic() - _t_start
    
    if _METRICS_AVAILABLE:
        if rewrites:
            QUERY_REWRITE_CALLS.labels(result="success").inc()
            QUERY_REWRITE_DURATION.observe(_duration)
            QUERY_REWRITE_VARIANTS.observe(len(rewrites))
        else:
            QUERY_REWRITE_CALLS.labels(result="error").inc()
    
    # Собираем итоговый список: original first, потом rewrites
    # Дедупликация на случай если Haiku вернул копию оригинала
    seen = {query.lower()}
    result = [query]
    for r in rewrites:
        if r.lower() not in seen:
            seen.add(r.lower())
            result.append(r)
    
    _cache_put(query, result)
    if _METRICS_AVAILABLE:
        QUERY_REWRITE_CACHE_SIZE.set(len(_cache))
    return result


# === CLI для smoke-теста ===
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv("/opt/ai/bridge/.env")
    
    logging.basicConfig(level=logging.DEBUG)
    
    async def smoke_test():
        test_queries = [
            "как уволиться?",
            "командировки",
            "налог на прибыль 2026",
            "процедура согласования отпуска по уходу за ребёнком",  # длинный — skip
            "DLP",
            "1С",
        ]
        
        print("=" * 60)
        print(f"QUERY_REWRITE={QUERY_REWRITE}, HAIKU_MODEL={HAIKU_MODEL}")
        print("=" * 60)
        
        for q in test_queries:
            print(f"\nЗапрос: '{q}' ({len(q.split())} слов)")
            import time
            start = time.time()
            result = await rewrite_query(q)
            duration = time.time() - start
            print(f"  Время: {duration*1000:.0f}ms")
            print(f"  Variants ({len(result)}):")
            for v in result:
                print(f"    - {v}")
            
            # Второй вызов — должен быть из кэша
            start = time.time()
            cached = await rewrite_query(q)
            duration = time.time() - start
            print(f"  Cache check: {duration*1000:.1f}ms (same result: {cached == result})")
    
    asyncio.run(smoke_test())
