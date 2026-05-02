#!/usr/bin/env python3
"""
v1.7.1 smoke tests.

Проверяет:
1. Импорт расширенного base.py (cache_*, history_log, render_chart_png)
2. Таблицы ai.report_cache и ai.report_history существуют
3. cache_make_key детерминирован (одинаковые params → одинаковый ключ)
4. cache_set + cache_get round-trip
5. ASCII bar-chart строится
6. /templates/history endpoint отвечает 200
"""
from __future__ import annotations

import asyncio
import os
import sys


PASS = "✅"
FAIL = "❌"
SKIP = "⏭"


def report(status: str, msg: str) -> None:
    print(f"{status} {msg}")


async def test_imports() -> bool:
    try:
        from app.templates.base import (
            cache_get, cache_set, cache_make_key, cache_cleanup_expired,
            history_log, render_chart_png,
        )
        from app.templates.weekly_infopovody import _ascii_bar_chart
        report(PASS, "v1.7.1 base imports OK")
        return True
    except Exception as e:
        report(FAIL, f"v1.7.1 imports: {e}")
        return False


async def test_tables() -> bool:
    import asyncpg
    dsn = os.environ.get("DATABASE_URL_AI")
    if not dsn:
        report(SKIP, "DATABASE_URL_AI not set")
        return True
    try:
        c = await asyncpg.connect(dsn)
        rows = await c.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='ai' "
            "AND tablename IN ('report_cache','report_history') ORDER BY tablename"
        )
        await c.close()
        names = [r["tablename"] for r in rows]
        if names != ["report_cache", "report_history"]:
            report(FAIL, f"Tables expected, got: {names}")
            return False
        report(PASS, f"Tables exist: {names}")
        return True
    except Exception as e:
        report(FAIL, f"tables check: {e}")
        return False


async def test_cache_key_deterministic() -> bool:
    from app.templates.base import cache_make_key
    k1 = cache_make_key("competitor_summary", {"competitor": "Гарант", "limit": 15})
    k2 = cache_make_key("competitor_summary", {"limit": 15, "competitor": "Гарант"})
    if k1 != k2:
        report(FAIL, f"cache_make_key non-deterministic: {k1} vs {k2}")
        return False
    if not k1.startswith("competitor_summary:"):
        report(FAIL, f"cache_make_key wrong prefix: {k1}")
        return False
    report(PASS, f"cache_make_key deterministic: {k1[:40]}…")
    return True


async def test_cache_roundtrip() -> bool:
    import asyncpg
    from app.templates.base import cache_get, cache_set, cache_make_key

    dsn = os.environ.get("DATABASE_URL_AI")
    if not dsn:
        report(SKIP, "DATABASE_URL_AI not set")
        return True

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        # Уникальный ключ для теста
        key = cache_make_key("__smoke__", {"x": 1})
        await cache_set(pool, key, "TEST_CONTENT", ttl_hours=1)
        v = await cache_get(pool, key)
        if v != "TEST_CONTENT":
            report(FAIL, f"cache roundtrip: stored 'TEST_CONTENT', got {v!r}")
            return False
        # Cleanup
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM ai.report_cache WHERE cache_key = $1", key)
        report(PASS, "cache set/get roundtrip OK")
        return True
    except Exception as e:
        report(FAIL, f"cache roundtrip: {e}")
        return False
    finally:
        await pool.close()


async def test_ascii_chart() -> bool:
    from app.templates.weekly_infopovody import _ascii_bar_chart
    text = _ascii_bar_chart(
        {"2026-04-25": 4, "2026-04-26": 8, "2026-04-27": 2},
        title="Test"
    )
    if "█" not in text or "Test" not in text:
        report(FAIL, f"ASCII chart wrong: {text!r}")
        return False
    if "2026-04-25" not in text:
        report(FAIL, "ASCII chart missing date")
        return False
    report(PASS, "ASCII bar-chart OK")
    return True


async def test_history_endpoint() -> bool:
    import httpx
    token = os.environ.get("BRIDGE_INTERNAL_TOKEN")
    if not token:
        report(FAIL, "BRIDGE_INTERNAL_TOKEN not in env")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "http://127.0.0.1:8284/templates/history",
                headers={"X-Internal-Token": token},
                json={"limit": 5},
            )
        if r.status_code != 200:
            report(FAIL, f"/templates/history HTTP {r.status_code}: {r.text[:200]}")
            return False
        data = r.json()
        if "count" not in data or "items" not in data:
            report(FAIL, f"/templates/history missing keys: {list(data.keys())}")
            return False
        report(PASS, f"/templates/history OK (count={data['count']})")
        return True
    except Exception as e:
        report(FAIL, f"/templates/history: {e}")
        return False


async def main() -> int:
    print("=" * 60)
    print("Leo v1.7.1 — Templates extras smoke tests")
    print("=" * 60)

    results = []
    results.append(await test_imports())
    results.append(await test_tables())
    results.append(await test_cache_key_deterministic())
    results.append(await test_cache_roundtrip())
    results.append(await test_ascii_chart())
    results.append(await test_history_endpoint())

    print("=" * 60)
    failed = sum(1 for r in results if not r)
    if failed:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        return 1
    print(f"{PASS} All {len(results)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
