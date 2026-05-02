"""
v1.7.1 шаблон #1: weekly_infopovody с ASCII-чартом по дням.

Изменения относительно v1.7.0:
- В начале документа добавлен ASCII bar-chart с распределением карточек по дням.
- Дни идут от старого к новому (хронология).
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


def _ascii_bar_chart(
    by_day: dict[str, int],
    title: str = "Распределение по дням",
    width: int = 30,
) -> str:
    """ASCII bar-chart, помещается в моноширинный блок Markdown.

    Пример:
        Распределение по дням
        ─────────────────────
        2026-04-25  ████████ 8
        2026-04-26  ████ 4
        2026-04-27  ████████████ 12
    """
    if not by_day:
        return ""
    max_v = max(by_day.values())
    if max_v == 0:
        return ""
    sorted_days = sorted(by_day.keys())  # хронология

    lines = ["```", title, "─" * len(title)]
    for day in sorted_days:
        v = by_day[day]
        bar_len = int(round(v / max_v * width))
        bar = "█" * max(bar_len, 1 if v > 0 else 0)
        lines.append(f"{day}  {bar} {v}")
    lines.append("```")
    return "\n".join(lines)


async def render(
    *,
    params: dict[str, Any],
    leo_pool: asyncpg.Pool,
    matrix_room_id: str,
    matrix_user_id: str,
) -> dict[str, Any]:
    weeks_back = int(params.get("weeks_back", 1))
    weeks_back = max(1, min(12, weeks_back))

    since = datetime.now(timezone.utc) - timedelta(days=7 * weeks_back)

    accessible_ids = await get_accessible_ids(matrix_user_id)
    if not accessible_ids:
        return _empty_doc(
            "Обзор инфоповодов",
            "У вас нет доступа к корпоративной KB Респект.Чата либо ACL недоступен.",
        )

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

    cids = [r["content_id"] for r in rows]
    atts_by_cid = await get_attachments_for_cards(leo_pool, cids)

    # Группировка
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        when = r["actualized_at"] or r["updated_at"]
        day_key = fmt_date(when)
        by_day[day_key].append(dict(r))

    # v1.7.1: данные для ASCII-чарта
    counts_by_day: dict[str, int] = {day: len(items) for day, items in by_day.items()}

    period_str = f"{fmt_date(since)} — {fmt_date(datetime.now(timezone.utc))}"

    lines: list[str] = []
    lines.append(f"# Обзор инфоповодов за {weeks_back} нед.")
    lines.append("")
    lines.append(f"**Период:** {period_str}  ")
    lines.append(f"**Найдено материалов:** {len(rows)}")
    lines.append("")

    # v1.7.1: ASCII chart
    chart = _ascii_bar_chart(counts_by_day, title="Распределение по дням")
    if chart:
        lines.append(chart)
        lines.append("")

    lines.append("---")
    lines.append("")

    for day in sorted(by_day.keys(), reverse=True):
        items = by_day[day]
        lines.append(f"## {day}  ({len(items)} материалов)")
        lines.append("")
        for item in items:
            lines.append(f"### {item['title']}")
            lines.append(f"*Раздел:* {format_section_path(item['section_path'])}  ")
            lines.append(f"*ID:* {item['content_id']}")
            lines.append("")

            body = (item.get("body_plain") or "").strip()
            if body:
                snippet = body[:400].replace("\n", " ")
                if len(body) > 400:
                    snippet += "…"
                lines.append(f"> {snippet}")
                lines.append("")

            atts = atts_by_cid.get(item["content_id"], [])
            if atts:
                lines.append("**Прикреплённые материалы:**")
                lines.append(format_attachments(atts))
                lines.append("")
            lines.append("")

        lines.append("---")
        lines.append("")

    content_md = "\n".join(lines)
    today = datetime.now().strftime("%Y%m%d")
    filename = f"infopovody_review_{today}_{weeks_back}w"

    return {
        "filename": filename,
        "format": "docx",
        "title": f"Обзор инфоповодов ({period_str})",
        "content_md": content_md,
    }


def _empty_doc(title: str, message: str) -> dict[str, Any]:
    md = f"# {title}\n\n{message}\n"
    return {
        "filename": title.lower().replace(" ", "_")[:40] + "_empty",
        "format": "md",
        "title": title,
        "content_md": md,
    }
