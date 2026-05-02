#!/usr/bin/env python3
"""
v1.8.1 smoke tests.

1. _calculate_period возвращает правильную прошлую завершённую Mon-Sun
2. _fmt_date_ru правильно форматирует на русском
3. Description calendar_summary содержит ключевые триггер-фразы
4. /templates/list endpoint содержит обновлённое описание
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone


PASS = "✅"
FAIL = "❌"


def report(status: str, msg: str) -> None:
    print(f"{status} {msg}")


async def test_period_calc() -> bool:
    from app.templates.calendar_summary import _calculate_period

    # weeks_back=1: должна быть прошлая Mon-Sun
    period_start, period_end = _calculate_period(1)

    if period_start.weekday() != 0:
        report(FAIL, f"period_start не понедельник: {period_start} weekday={period_start.weekday()}")
        return False
    if period_end.weekday() != 0:
        report(FAIL, f"period_end не понедельник: {period_end}")
        return False

    delta = period_end - period_start
    if delta != timedelta(weeks=1):
        report(FAIL, f"weeks_back=1 даёт период {delta}, ожидалось 7 дней")
        return False

    # weeks_back=2: 14 дней
    p2_start, p2_end = _calculate_period(2)
    if p2_end - p2_start != timedelta(weeks=2):
        report(FAIL, f"weeks_back=2 даёт {p2_end - p2_start}, ожидалось 14 дней")
        return False

    report(PASS, f"_calculate_period(1) = {period_start.date()} → {period_end.date()}")
    return True


async def test_fmt_date_ru() -> bool:
    from app.templates.calendar_summary import _fmt_date_ru

    test_dt = datetime(datetime.now().year, 4, 20, tzinfo=timezone.utc)
    result = _fmt_date_ru(test_dt)
    if result != "20 апреля":
        report(FAIL, f"_fmt_date_ru gave {result!r}, expected '20 апреля'")
        return False

    # Год отличается от текущего → должен быть в выводе
    test_old = datetime(2020, 4, 20, tzinfo=timezone.utc)
    result_old = _fmt_date_ru(test_old)
    if "2020" not in result_old:
        report(FAIL, f"_fmt_date_ru для прошлого года не показал год: {result_old!r}")
        return False

    report(PASS, "_fmt_date_ru OK")
    return True


async def test_description_keywords() -> bool:
    from app.templates import TEMPLATES
    desc = TEMPLATES["calendar_summary"]["description"]
    keywords = ["обзор календаря", "отчёт", "DOCX", "ретроспектив"]
    missing = [k for k in keywords if k not in desc]
    if missing:
        report(FAIL, f"description missing keywords: {missing}")
        return False
    report(PASS, "calendar_summary description содержит триггер-фразы")
    return True


async def test_endpoint_returns_new_desc() -> bool:
    import httpx
    token = os.environ.get("BRIDGE_INTERNAL_TOKEN")
    if not token:
        report(FAIL, "BRIDGE_INTERNAL_TOKEN not in env")
        return False
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            "http://127.0.0.1:8284/templates/list",
            headers={"X-Internal-Token": token},
            json={},
        )
    if r.status_code != 200:
        report(FAIL, f"HTTP {r.status_code}")
        return False
    data = r.json()
    cal = next((t for t in data["templates"] if t["name"] == "calendar_summary"), None)
    if cal is None:
        report(FAIL, "calendar_summary not in list")
        return False
    if "DOCX" not in cal["description"]:
        report(FAIL, f"endpoint description без DOCX: {cal['description'][:200]}")
        return False
    report(PASS, "/templates/list возвращает обновлённое description")
    return True


async def main() -> int:
    print("=" * 60)
    print("Leo v1.8.1 — calendar_summary fix smoke tests")
    print("=" * 60)

    results = []
    results.append(await test_period_calc())
    results.append(await test_fmt_date_ru())
    results.append(await test_description_keywords())
    results.append(await test_endpoint_returns_new_desc())

    print("=" * 60)
    failed = sum(1 for r in results if not r)
    if failed:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        return 1
    print(f"{PASS} All {len(results)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
