"""
v1.7.0 шаблон #2: kb_changes_digest.

Что изменилось в KB за последние N дней — фильтр по updated_at.
Группировка по корневому разделу (последний элемент section_path в нашей схеме).
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
    get_root_section,
)


async def render(
    *,
    params: dict[str, Any],
    leo_pool: asyncpg.Pool,
    matrix_room_id: str,
    matrix_user_id: str,
) -> dict[str, Any]:
    days_back = int(params.get("days_back", 7))
    days_back = max(1, min(90, days_back))

    since = datetime.now(timezone.utc) - timedelta(days=days_back)

    # ACL
    accessible_ids = await get_accessible_ids(matrix_user_id)
    if not accessible_ids:
        return _empty_doc(
            "Дайджест изменений KB",
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
        WHERE content_id = ANY($1::bigint[])
          AND updated_at >= $2
        ORDER BY updated_at DESC, content_id DESC
        LIMIT 500
    """
    async with leo_pool.acquire() as conn:
        rows = await conn.fetch(sql, accessible_ids, since)

    if not rows:
        return _empty_doc(
            f"Дайджест изменений KB за {days_back} дн.",
            f"За последние {days_back} дн. изменений в KB не зафиксировано.",
        )

    # Группировка по корневому разделу
    by_root: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        root = get_root_section(r["section_path"])
        by_root[root].append(dict(r))

    cids = [r["content_id"] for r in rows]
    atts_by_cid = await get_attachments_for_cards(leo_pool, cids)

    period_str = f"{fmt_date(since)} — {fmt_date(datetime.now(timezone.utc))}"

    lines: list[str] = []
    lines.append(f"# Дайджест изменений в корпоративной KB")
    lines.append("")
    lines.append(f"**Период:** {period_str}  ")
    lines.append(f"**Изменено материалов:** {len(rows)}  ")
    lines.append(f"**Затронутых разделов:** {len(by_root)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Сортируем разделы по убыванию количества изменений
    sorted_roots = sorted(by_root.items(), key=lambda x: -len(x[1]))

    for root, items in sorted_roots:
        lines.append(f"## {root} ({len(items)})")
        lines.append("")
        for item in items[:50]:  # ограничение на раздел чтобы документ не разросся
            cid = item["content_id"]
            updated = fmt_date(item["updated_at"])
            actualized = fmt_date(item["actualized_at"])
            lines.append(f"### {item['title']}")
            lines.append(f"*Полный путь:* {format_section_path(item['section_path'])}  ")
            lines.append(f"*Обновлено:* {updated} · *Актуализация:* {actualized} · *ID:* {cid}")
            lines.append("")

            body = (item.get("body_plain") or "").strip()
            if body:
                snippet = body[:300].replace("\n", " ")
                if len(body) > 300:
                    snippet += "…"
                lines.append(f"> {snippet}")
                lines.append("")

            atts = atts_by_cid.get(cid, [])
            if atts:
                lines.append(format_attachments(atts[:5]))  # макс 5 файлов на карточку
                if len(atts) > 5:
                    lines.append(f"  *(и ещё {len(atts) - 5} файлов)*")
                lines.append("")

        if len(items) > 50:
            lines.append(f"*…и ещё {len(items) - 50} материалов в этом разделе*")
            lines.append("")
        lines.append("---")
        lines.append("")

    content_md = "\n".join(lines)
    today = datetime.now().strftime("%Y%m%d")
    return {
        "filename": f"kb_changes_{today}_{days_back}d",
        "format": "docx",
        "title": f"Дайджест изменений KB ({period_str})",
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
