"""
v1.7.0 шаблон #1: weekly_infopovody.

Берёт все карточки из раздела ИНФОПОВОДЫ за последние N недель,
группирует по дате актуализации (по дням), формирует Markdown.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from app.templates.base import (
    fmt_date,
    format_attachments,
    format_section_path,
    get_accessible_ids,
    get_attachments_for_cards,
)


async def render(
    *,
    params: dict[str, Any],
    leo_pool: asyncpg.Pool,
    matrix_room_id: str,
    matrix_user_id: str,
) -> dict[str, Any]:
    """Сформировать еженедельный обзор инфоповодов."""

    weeks_back = int(params.get("weeks_back", 1))
    weeks_back = max(1, min(12, weeks_back))

    since = datetime.now(timezone.utc) - timedelta(days=7 * weeks_back)

    # 1. ACL
    accessible_ids = await get_accessible_ids(matrix_user_id)
    if not accessible_ids:
        return _empty_doc(
            f"Обзор инфоповодов",
            "У вас нет доступа к корпоративной KB Респект.Чата либо ACL недоступен.",
        )

    # 2. SQL
    sql = """
        SELECT
            content_id,
            title,
            body_plain,
            section_path,
            actualized_at,
            updated_at
        FROM ai.respect_kb
        WHERE 'ИНФОПОВОДЫ' = ANY(section_path)
          AND content_id = ANY($1::bigint[])
          AND COALESCE(actualized_at, updated_at) >= $2
        ORDER BY COALESCE(actualized_at, updated_at) DESC, content_id DESC
    """
    async with leo_pool.acquire() as conn:
        rows = await conn.fetch(sql, accessible_ids, since)

    if not rows:
        return _empty_doc(
            f"Обзор инфоповодов за {weeks_back} нед.",
            f"За последние {weeks_back} нед. в разделе ИНФОПОВОДЫ материалов не найдено.",
        )

    # 3. Подгружаем attachments
    cids = [r["content_id"] for r in rows]
    atts_by_cid = await get_attachments_for_cards(leo_pool, cids)

    # 4. Группировка по дате (день)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        when = r["actualized_at"] or r["updated_at"]
        day_key = fmt_date(when)
        by_day[day_key].append(dict(r))

    # 5. Markdown
    period_str = (
        f"{fmt_date(since)} — {fmt_date(datetime.now(timezone.utc))}"
    )

    lines: list[str] = []
    lines.append(f"# Обзор инфоповодов за {weeks_back} нед.")
    lines.append("")
    lines.append(f"**Период:** {period_str}  ")
    lines.append(f"**Найдено материалов:** {len(rows)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for day in sorted(by_day.keys(), reverse=True):
        items = by_day[day]
        lines.append(f"## {day}")
        lines.append("")
        for item in items:
            lines.append(f"### {item['title']}")
            lines.append(f"*Раздел:* {format_section_path(item['section_path'])}  ")
            lines.append(f"*ID:* {item['content_id']}")
            lines.append("")

            # Краткий фрагмент тела (первые 400 символов)
            body = (item.get("body_plain") or "").strip()
            if body:
                snippet = body[:400].replace("\n", " ")
                if len(body) > 400:
                    snippet += "…"
                lines.append(f"> {snippet}")
                lines.append("")

            # Прикреплённые файлы
            atts = atts_by_cid.get(item["content_id"], [])
            if atts:
                lines.append("**Прикреплённые материалы:**")
                lines.append(format_attachments(atts))
                lines.append("")

            lines.append("")  # разделитель между карточками

        lines.append("---")
        lines.append("")

    content_md = "\n".join(lines)

    # 6. Имя файла
    today = datetime.now().strftime("%Y%m%d")
    filename = f"infopovody_review_{today}_{weeks_back}w"

    return {
        "filename": filename,
        "format": "docx",
        "title": f"Обзор инфоповодов ({period_str})",
        "content_md": content_md,
    }


def _empty_doc(title: str, message: str) -> dict[str, Any]:
    """Краткий «пустой» отчёт когда данных нет."""
    md = f"# {title}\n\n{message}\n"
    return {
        "filename": title.lower().replace(" ", "_")[:40] + "_empty",
        "format": "md",
        "title": title,
        "content_md": md,
    }
