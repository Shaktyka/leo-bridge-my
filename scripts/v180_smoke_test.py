#!/usr/bin/env python3
"""
v1.8.0 smoke tests.

1. Импорт calendar_summary
2. TEMPLATES содержит 5 записей (включая calendar_summary)
3. render() пробрасывает cal_client когда renderer принимает этот параметр
4. /templates/list endpoint возвращает 5 шаблонов
5. _stats_from_events корректно считает на синтетических данных
6. _ascii_chart строится
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone


PASS = "✅"
FAIL = "❌"
SKIP = "⏭"


def report(status: str, msg: str) -> None:
    print(f"{status} {msg}")


async def test_imports() -> bool:
    try:
        from app.templates import calendar_summary, TEMPLATES, render, list_templates
        from app.templates.calendar_summary import (
            _stats_from_events, _ascii_chart, _format_event_line, _weekday_ru,
        )
        report(PASS, "v1.8.0 imports OK")
        return True
    except Exception as e:
        report(FAIL, f"imports: {e}")
        return False


async def test_registry_has_5() -> bool:
    from app.templates import TEMPLATES, list_templates
    expected = {
        "weekly_infopovody", "kb_changes_digest", "competitor_summary",
        "topic_compendium", "calendar_summary",
    }
    actual = set(TEMPLATES.keys())
    if actual != expected:
        report(FAIL, f"Registry mismatch: missing={expected - actual}, extra={actual - expected}")
        return False
    if len(list_templates()) != 5:
        report(FAIL, f"list_templates() returned {len(list_templates())} items")
        return False
    report(PASS, f"Registry has 5 templates including calendar_summary")
    return True


async def test_endpoint_list_5() -> bool:
    import httpx
    token = os.environ.get("BRIDGE_INTERNAL_TOKEN")
    if not token:
        report(FAIL, "BRIDGE_INTERNAL_TOKEN not in env")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "http://127.0.0.1:8284/templates/list",
                headers={"X-Internal-Token": token},
                json={},
            )
        if r.status_code != 200:
            report(FAIL, f"/templates/list HTTP {r.status_code}: {r.text[:200]}")
            return False
        data = r.json()
        names = [t["name"] for t in data.get("templates", [])]
        if "calendar_summary" not in names:
            report(FAIL, f"calendar_summary not in templates list: {names}")
            return False
        report(PASS, f"/templates/list endpoint OK ({len(names)} templates)")
        return True
    except Exception as e:
        report(FAIL, f"/templates/list: {e}")
        return False


async def test_stats() -> bool:
    from app.templates.calendar_summary import _stats_from_events

    # Синтетические события: 2 ежедневных stand-up + 1 длинная встреча
    events = [
        {"start": "2026-04-25T09:00:00+00:00", "end": "2026-04-25T09:15:00+00:00", "title": "Stand-up"},
        {"start": "2026-04-26T09:00:00+00:00", "end": "2026-04-26T09:15:00+00:00", "title": "Stand-up"},
        {"start": "2026-04-27T09:00:00+00:00", "end": "2026-04-27T09:15:00+00:00", "title": "Stand-up"},
        {"start": "2026-04-25T14:00:00+00:00", "end": "2026-04-25T16:00:00+00:00", "title": "Big meeting"},
    ]
    s = _stats_from_events(events)
    if s["count"] != 4:
        report(FAIL, f"count expected 4, got {s['count']}")
        return False
    # 3 stand-up по 15 min = 45m + 1 big = 120m. total = 165m
    if s["total_minutes"] != 165:
        report(FAIL, f"total_minutes expected 165, got {s['total_minutes']}")
        return False
    if s["longest_minutes"] != 120:
        report(FAIL, f"longest expected 120, got {s['longest_minutes']}")
        return False
    if not s["repeating"] or s["repeating"][0][0] != "stand-up":
        report(FAIL, f"repeating expected stand-up, got {s['repeating']}")
        return False
    report(PASS, f"stats OK (count={s['count']}, total={s['total_minutes']}m, repeating={s['repeating']})")
    return True


async def test_ascii_chart() -> bool:
    from app.templates.calendar_summary import _ascii_chart
    chart = _ascii_chart({"2026-04-25": 3, "2026-04-26": 5})
    if "█" not in chart or "Встречи" not in chart:
        report(FAIL, f"ASCII chart wrong: {chart!r}")
        return False
    report(PASS, "ASCII chart OK")
    return True


async def test_render_cal_passthrough() -> bool:
    """Проверка что render() пробрасывает cal_client когда renderer его требует.

    Не делаем реальный рендер — просто sanity check сигнатур.
    """
    import inspect
    from app.templates import calendar_summary, weekly_infopovody

    sig_cal = inspect.signature(calendar_summary.render)
    if "cal_client" not in sig_cal.parameters:
        report(FAIL, "calendar_summary.render doesn't accept cal_client")
        return False

    sig_kb = inspect.signature(weekly_infopovody.render)
    if "cal_client" in sig_kb.parameters:
        report(FAIL, "weekly_infopovody.render shouldn't accept cal_client (KB-only)")
        return False

    report(PASS, "render signature passthrough OK")
    return True


async def main() -> int:
    print("=" * 60)
    print("Leo v1.8.0 — calendar_summary smoke tests")
    print("=" * 60)

    results = []
    results.append(await test_imports())
    results.append(await test_registry_has_5())
    results.append(await test_endpoint_list_5())
    results.append(await test_stats())
    results.append(await test_ascii_chart())
    results.append(await test_render_cal_passthrough())

    print("=" * 60)
    failed = sum(1 for r in results if not r)
    if failed:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        return 1
    print(f"{PASS} All {len(results)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
