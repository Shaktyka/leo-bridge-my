"""
v1.7.0 шаблон #4: topic_compendium.

FTS-поиск по теме, группировка результатов по корневому разделу,
формирование структурированной подборки в docx.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
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
    topic = (params.get("topic") or "").strip()
    if not topic:
        return _empty_doc(
            "Подборка по теме",
            "Не указан параметр topic. Пример: 'командировки', 'НДФЛ'.",
        )

    limit = int(params.get("limit", 20))
    limit = max(1, min(50, limit))

    # ACL
    accessible_ids = await get_accessible_ids(matrix_user_id)
    if not accessible_ids:
        return _empty_doc(
            f"Подборка по теме «{topic}»",
            "У вас нет доступа к корпоративной KB Респект.Чата либо ACL недоступен.",
        )

    sql = """
        SELECT
            content_id,
            title,
            section_path,
            actualized_at,
            ts_rank(fts, websearch_to_tsquery('russian', $1)) AS rank,
            ts_headline(
                'russian',
                indexable_text,
                websearch_to_tsquery('russian', $1),
                'MaxFragments=2, MaxWords=25, MinWords=10, ShortWord=3, '
                'StartSel=**, StopSel=**'
            ) AS snippet
        FROM ai.respect_kb
        WHERE content_id = ANY($2::bigint[])
          AND fts @@ websearch_to_tsquery('russian', $1)
        ORDER BY rank DESC, actualized_at DESC NULLS LAST
        LIMIT $3
    """
    async with leo_pool.acquire() as conn:
        rows = await conn.fetch(sql, topic, accessible_ids, limit)

    if not rows:
        return _empty_doc(
            f"Подборка по теме «{topic}»",
            f"По теме «{topic}» в KB ничего не найдено.",
        )

    # Группировка по корневому разделу
    by_root: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        root = get_root_section(r["section_path"])
        by_root[root].append(dict(r))

    cids = [r["content_id"] for r in rows]
    atts_by_cid = await get_attachments_for_cards(leo_pool, cids)

    lines: list[str] = []
    lines.append(f"# Подборка по теме: «{topic}»")
    lines.append("")
    lines.append(
        f"**Найдено материалов:** {len(rows)}  \n"
        f"**Затронутых разделов:** {len(by_root)}"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Сортировка разделов по количеству результатов
    sorted_roots = sorted(by_root.items(), key=lambda x: -len(x[1]))

    for root, items in sorted_roots:
        lines.append(f"## {root} ({len(items)})")
        lines.append("")
        # Внутри раздела сортируем по rank
        items.sort(key=lambda x: -float(x["rank"]))
        for item in items:
            cid = item["content_id"]
            lines.append(f"### {item['title']}")
            lines.append(
                f"*Полный путь:* {format_section_path(item['section_path'])}  \n"
                f"*Дата актуализации:* {fmt_date(item['actualized_at'])} · "
                f"*ID:* {cid} · "
                f"*Релевантность:* {float(item['rank']):.2f}"
            )
            lines.append("")

            snippet = (item.get("snippet") or "").strip().replace("\n", " ")
            if snippet:
                lines.append(f"> {snippet}")
                lines.append("")

            atts = atts_by_cid.get(cid, [])
            if atts:
                lines.append(format_attachments(atts[:5]))
                if len(atts) > 5:
                    lines.append(f"  *(и ещё {len(atts) - 5} файлов)*")
                lines.append("")

        lines.append("---")
        lines.append("")

    content_md = "\n".join(lines)
    today = datetime.now().strftime("%Y%m%d")
    safe_topic = "".join(c if c.isalnum() else "_" for c in topic)[:30]
    return {
        "filename": f"compendium_{safe_topic}_{today}",
        "format": "docx",
        "title": f"Подборка по теме «{topic}»",
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
