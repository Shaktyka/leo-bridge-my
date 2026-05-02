"""
v1.7.0 шаблон #3: competitor_summary.

FTS-поиск всех материалов про указанного конкурента, далее
отправка топ-N карточек в GPT-4o-mini для формирования сводки тезисов.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from app.templates.base import (
    fmt_date,
    format_attachments,
    format_section_path,
    get_accessible_ids,
    get_attachments_for_cards,
    gpt_summarize,
)


SYSTEM_PROMPT = (
    "Ты бизнес-аналитик в компании Respect.Chat. Твоя задача — на основе "
    "корпоративных материалов про конкурента сформулировать чёткие тезисы "
    "по структуре:\n\n"
    "1. Сильные стороны конкурента\n"
    "2. Слабые стороны конкурента\n"
    "3. Наши преимущества (Respect.Chat / КонсультантПлюс)\n"
    "4. Ключевые аргументы для разговора с клиентом\n"
    "5. Известные риски / возражения клиентов\n\n"
    "Пиши кратко и по делу. Только то что есть в материалах. "
    "Если по какой-то секции данных нет — пиши «Не покрыто в материалах». "
    "Не выдумывай факты. Используй Markdown с заголовками ##."
)


async def render(
    *,
    params: dict[str, Any],
    leo_pool: asyncpg.Pool,
    matrix_room_id: str,
    matrix_user_id: str,
) -> dict[str, Any]:
    competitor = (params.get("competitor") or "").strip()
    if not competitor:
        return _empty_doc(
            "Сводка по конкуренту",
            "Не указан параметр competitor. Пример: 'Гарант', 'Актион'.",
        )

    limit = int(params.get("limit", 15))
    limit = max(1, min(50, limit))

    # ACL
    accessible_ids = await get_accessible_ids(matrix_user_id)
    if not accessible_ids:
        return _empty_doc(
            f"Сводка по конкуренту {competitor}",
            "У вас нет доступа к корпоративной KB Респект.Чата либо ACL недоступен.",
        )

    # FTS-поиск с весами и snippet
    sql = """
        SELECT
            content_id,
            title,
            indexable_text,
            section_path,
            actualized_at,
            ts_rank(fts, websearch_to_tsquery('russian', $1)) AS rank
        FROM ai.respect_kb
        WHERE content_id = ANY($2::bigint[])
          AND fts @@ websearch_to_tsquery('russian', $1)
        ORDER BY rank DESC, actualized_at DESC NULLS LAST
        LIMIT $3
    """
    async with leo_pool.acquire() as conn:
        rows = await conn.fetch(sql, competitor, accessible_ids, limit)

    if not rows:
        return _empty_doc(
            f"Сводка по конкуренту {competitor}",
            f"По конкуренту «{competitor}» в KB ничего не найдено.",
        )

    # Готовим контекст для LLM (обрезаем тело до разумного размера)
    MAX_BODY_PER_CARD = 1500  # символов
    cards_for_llm: list[str] = []
    for i, r in enumerate(rows, 1):
        body = (r.get("indexable_text") or "").strip()
        if len(body) > MAX_BODY_PER_CARD:
            body = body[:MAX_BODY_PER_CARD] + "…"
        cards_for_llm.append(
            f"## Материал {i}: {r['title']}\n"
            f"*Раздел:* {format_section_path(r['section_path'])}\n"
            f"*Дата:* {fmt_date(r['actualized_at'])}\n\n"
            f"{body}"
        )

    user_prompt = (
        f"Конкурент: **{competitor}**\n\n"
        f"Ниже {len(cards_for_llm)} материалов из корпоративной базы знаний. "
        f"Сформулируй сводку по структуре из system-промпта.\n\n"
        + "\n\n---\n\n".join(cards_for_llm)
    )

    summary_md = await gpt_summarize(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=2000,
    )

    # Подгружаем attachments для приложений
    cids = [r["content_id"] for r in rows]
    atts_by_cid = await get_attachments_for_cards(leo_pool, cids)

    # Финальный документ
    lines: list[str] = []
    lines.append(f"# Сводка по конкуренту: {competitor}")
    lines.append("")
    lines.append(
        f"*Сгенерировано на основе {len(rows)} материалов из корпоративной KB. "
        f"Анализ выполнен LLM (GPT-4o-mini). Проверяйте перед использованием.*"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(summary_md)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"## Использованные материалы ({len(rows)})")
    lines.append("")

    for i, r in enumerate(rows, 1):
        cid = r["content_id"]
        lines.append(f"### {i}. {r['title']}")
        lines.append(
            f"*Раздел:* {format_section_path(r['section_path'])} · "
            f"*Дата:* {fmt_date(r['actualized_at'])} · "
            f"*ID:* {cid} · "
            f"*Релевантность:* {float(r['rank']):.2f}"
        )
        atts = atts_by_cid.get(cid, [])
        if atts:
            lines.append("")
            lines.append(format_attachments(atts[:5]))
            if len(atts) > 5:
                lines.append(f"  *(и ещё {len(atts) - 5} файлов)*")
        lines.append("")

    content_md = "\n".join(lines)
    today = datetime.now().strftime("%Y%m%d")
    safe_competitor = "".join(
        c if c.isalnum() else "_" for c in competitor
    )[:30]

    return {
        "filename": f"competitor_{safe_competitor}_{today}",
        "format": "docx",
        "title": f"Сводка по конкуренту: {competitor}",
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
