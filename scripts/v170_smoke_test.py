#!/usr/bin/env python3
"""
v1.7.0 smoke tests.

Проверяет:
1. Импорт всех 4 шаблонов
2. Реестр TEMPLATES содержит 4 записи
3. /templates/list endpoint отвечает 200
4. (опц.) рендер topic_compendium на тестовый запрос — без отправки в Matrix

Запуск:
    cd /opt/ai/bridge && set -a && source .env && set +a && \\
        PYTHONPATH=/opt/ai/bridge venv/bin/python scripts/v170_smoke_test.py
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
        from app.templates import TEMPLATES, list_templates, render
        from app.templates import (
            weekly_infopovody, kb_changes_digest,
            competitor_summary, topic_compendium,
        )
        from app.templates.base import (
            get_accessible_ids, get_attachments_for_cards,
            format_section_path, format_attachments,
            gpt_summarize, fmt_date, get_root_section,
        )
        report(PASS, "Templates modules import")
        return True
    except Exception as e:
        report(FAIL, f"Templates import: {e}")
        return False


async def test_registry() -> bool:
    from app.templates import TEMPLATES, list_templates
    expected = {"weekly_infopovody", "kb_changes_digest", "competitor_summary", "topic_compendium"}
    actual = set(TEMPLATES.keys())
    if actual != expected:
        report(FAIL, f"Registry mismatch: expected {expected}, got {actual}")
        return False
    tlist = list_templates()
    if len(tlist) != 4:
        report(FAIL, f"list_templates() returned {len(tlist)} items, expected 4")
        return False
    for t in tlist:
        if not t.get("name") or not t.get("description"):
            report(FAIL, f"Template missing name/description: {t}")
            return False
    report(PASS, f"Registry has {len(tlist)} templates")
    return True


async def test_endpoint_list() -> bool:
    """/templates/list через HTTP, как будет дёргать tool."""
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
        n = len(data.get("templates", []))
        if n != 4:
            report(FAIL, f"/templates/list returned {n} templates, expected 4")
            return False
        report(PASS, f"/templates/list endpoint OK ({n} templates)")
        return True
    except Exception as e:
        report(FAIL, f"/templates/list: {e}")
        return False


async def test_render_dry() -> bool:
    """Пробуем отрендерить topic_compendium на ненастоящего юзера.

    ACL вернёт пустой список → шаблон отдаст _empty_doc — и это нормальный exit.
    Тестируем что код не падает и формирует валидный output.
    """
    import asyncpg
    from app.templates import render

    dsn = os.environ.get("DATABASE_URL_AI")
    if not dsn:
        report(SKIP, "DATABASE_URL_AI not set, skip render test")
        return True

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        result = await render(
            name="topic_compendium",
            params={"topic": "тестовый_запрос_который_не_найдёт_ничего", "limit": 5},
            leo_pool=pool,
            matrix_room_id="!fake:test",
            matrix_user_id="@nonexistent_test_user:respectrb.ru",
        )
        if not result.get("content_md"):
            report(FAIL, "render returned no content_md")
            return False
        if not result.get("filename"):
            report(FAIL, "render returned no filename")
            return False
        report(PASS, f"render dry-run OK (got {len(result['content_md'])} chars md)")
        return True
    except Exception as e:
        report(FAIL, f"render dry-run: {e}")
        return False
    finally:
        await pool.close()


async def main() -> int:
    print("=" * 60)
    print("Leo v1.7.0 — Templates smoke tests")
    print("=" * 60)

    results = []
    results.append(await test_imports())
    results.append(await test_registry())
    results.append(await test_endpoint_list())
    results.append(await test_render_dry())

    print("=" * 60)
    failed = sum(1 for r in results if not r)
    if failed:
        print(f"{FAIL} {failed}/{len(results)} tests failed")
        return 1
    print(f"{PASS} All {len(results)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
