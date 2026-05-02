"""
v1.7.0: Общие хелперы для шаблонов.

Содержит:
- access_id_filter: получение списка доступных content_id для юзера
- get_attachments_for_cards: подгрузка attachments для списка карточек
- format_section_path: красивое отображение section_path (от корня к листу)
- gpt_summarize: суммаризация текста через GPT-4o-mini
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import asyncpg
import httpx


log = logging.getLogger("templates.base")


SUMMARY_MODEL = os.environ.get("LEO_TEMPLATES_GPT_MODEL", "gpt-4o-mini")
SUMMARY_TIMEOUT = float(os.environ.get("LEO_TEMPLATES_GPT_TIMEOUT_S", "60"))


# ----------------------------------------------------------------------------
# ACL: получить доступные content_id для пользователя
# ----------------------------------------------------------------------------
async def get_accessible_ids(matrix_user_id: str) -> list[int]:
    """Получить список content_id, доступных пользователю.

    Использует respect_db.RespectDBClient (если RESPECT_DATABASE_URL задан),
    иначе возвращает пустой список (templates fail-closed).
    """
    if not os.environ.get("RESPECT_DATABASE_URL"):
        log.warning("RESPECT_DATABASE_URL not set, ACL check unavailable")
        return []

    # Один-разовый коннект (избегаем лишних зависимостей от shared pool)
    from app.respect_db import RespectDBClient
    try:
        async with RespectDBClient.from_env() as client:
            return await client.get_accessible_content_ids(matrix_user_id)
    except Exception as e:
        log.warning("ACL fetch failed for %s: %s", matrix_user_id, e)
        return []


# ----------------------------------------------------------------------------
# Attachments: подгрузка для списка карточек
# ----------------------------------------------------------------------------
async def get_attachments_for_cards(
    leo_pool: asyncpg.Pool,
    content_ids: list[int],
) -> dict[int, list[dict]]:
    """Вернуть {content_id: [{name, url, type}, ...]}.

    Только реальные ссылки. Карточки без attachments в результате не присутствуют.
    """
    if not content_ids:
        return {}

    sql = """
        SELECT
            ca.content_id,
            ca.display_name AS name,
            ca.display_url  AS url,
            a.file_type
        FROM ai.respect_kb_content_attachments ca
        JOIN ai.respect_kb_attachments a USING (sha256)
        WHERE ca.content_id = ANY($1::bigint[])
        ORDER BY ca.content_id, ca.display_name
    """
    async with leo_pool.acquire() as conn:
        rows = await conn.fetch(sql, content_ids)

    result: dict[int, list[dict]] = {}
    for r in rows:
        result.setdefault(r["content_id"], []).append({
            "name": r["name"],
            "url":  r["url"],
            "type": r["file_type"],
        })
    return result


# ----------------------------------------------------------------------------
# Форматирование
# ----------------------------------------------------------------------------
def format_section_path(section_path: list[str] | None, max_depth: int = 4) -> str:
    """Превратить section_path в читаемый путь.

    section_path в БД хранится от листа к корню — здесь разворачиваем
    к человекочитаемому виду 'Корень → Подраздел → ... → Лист'.

    max_depth: показать максимум N уровней (от корня), остальное — "...".
    """
    if not section_path:
        return "—"
    # Разворачиваем
    reversed_path = list(reversed(section_path))
    if len(reversed_path) > max_depth:
        # Берём первые max_depth уровней + многоточие
        shown = reversed_path[:max_depth]
        return " → ".join(shown) + " → …"
    return " → ".join(reversed_path)


def get_root_section(section_path: list[str] | None) -> str:
    """Получить корневой раздел (последний элемент массива в нашей схеме)."""
    if not section_path:
        return "—"
    return section_path[-1]


_FILE_ICONS = {
    "pdf": "📄", "docx": "📄", "doc": "📄", "txt": "📄",
    "md": "📄", "html": "📄", "rtf": "📄",
    "xlsx": "📊", "xls": "📊", "csv": "📊",
    "pptx": "📽", "ppt": "📽",
    "video": "🎬", "audio": "🎵",
    "image": "🖼",
    "other": "📎",
}


def format_attachments(attachments: list[dict]) -> str:
    """Форматировать список вложений в Markdown-список со ссылками."""
    if not attachments:
        return ""
    lines = []
    for att in attachments:
        icon = _FILE_ICONS.get((att.get("type") or "").lower(), "📎")
        name = att.get("name", "файл")
        url = att.get("url", "")
        if url:
            lines.append(f"- {icon} [{name}]({url})")
        else:
            lines.append(f"- {icon} {name}")
    return "\n".join(lines)


def fmt_date(dt: datetime | None) -> str:
    """Дата в коротком виде YYYY-MM-DD."""
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# GPT-4o-mini summarization
# ----------------------------------------------------------------------------
async def gpt_summarize(
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
) -> str:
    """Дёрнуть GPT-4o-mini для суммаризации.

    Возвращает текст ответа (Markdown). Если API упал — возвращает строку с ошибкой.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "_(OPENAI_API_KEY не настроен — суммаризация недоступна)_"

    body = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=SUMMARY_TIMEOUT) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if r.status_code >= 400:
            log.warning("GPT API %s: %s", r.status_code, r.text[:300])
            return f"_(ошибка LLM API: {r.status_code})_"
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip() or "_(пустой ответ LLM)_"
    except Exception as e:
        log.exception("GPT call failed: %s", e)
        return f"_(ошибка вызова LLM: {e})_"
