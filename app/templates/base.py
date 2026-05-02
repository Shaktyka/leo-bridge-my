"""
v1.7.1: Общие хелперы для шаблонов.

Дополнения относительно v1.7.0:
- cache_get / cache_set / cache_make_key — TTL-кеш в ai.report_cache
- history_log — запись в ai.report_history
- render_chart_png — генерация PNG-графика (matplotlib) для встраивания в docx
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx


log = logging.getLogger("templates.base")


SUMMARY_MODEL = os.environ.get("LEO_TEMPLATES_GPT_MODEL", "gpt-4o-mini")
SUMMARY_TIMEOUT = float(os.environ.get("LEO_TEMPLATES_GPT_TIMEOUT_S", "60"))

# v1.7.1: TTL для кеша в часах (по умолчанию 24)
CACHE_TTL_HOURS = int(os.environ.get("LEO_TEMPLATES_CACHE_TTL_HOURS", "24"))


# ----------------------------------------------------------------------------
# ACL
# ----------------------------------------------------------------------------
async def get_accessible_ids(matrix_user_id: str) -> list[int]:
    if not os.environ.get("RESPECT_DATABASE_URL"):
        log.warning("RESPECT_DATABASE_URL not set, ACL check unavailable")
        return []

    from app.respect_db import RespectDBClient
    try:
        async with RespectDBClient.from_env() as client:
            return await client.get_accessible_content_ids(matrix_user_id)
    except Exception as e:
        log.warning("ACL fetch failed for %s: %s", matrix_user_id, e)
        return []


# ----------------------------------------------------------------------------
# Attachments
# ----------------------------------------------------------------------------
async def get_attachments_for_cards(
    leo_pool: asyncpg.Pool,
    content_ids: list[int],
) -> dict[int, list[dict]]:
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
    if not section_path:
        return "—"
    reversed_path = list(reversed(section_path))
    if len(reversed_path) > max_depth:
        shown = reversed_path[:max_depth]
        return " → ".join(shown) + " → …"
    return " → ".join(reversed_path)


def get_root_section(section_path: list[str] | None) -> str:
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
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# v1.7.1: Кеш с TTL
# ----------------------------------------------------------------------------
def cache_make_key(template_name: str, params: dict[str, Any]) -> str:
    """Стабильный ключ кеша: <template>:<sha256(canonical_json_params)>.

    Параметры сортируются по ключам для канонической формы.
    """
    canon = json.dumps(params, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]
    return f"{template_name}:{h}"


async def cache_get(
    leo_pool: asyncpg.Pool,
    cache_key: str,
) -> str | None:
    """Получить content_md из кеша если есть и не устарел.

    При hit инкрементирует hit_count.
    """
    sql = """
        UPDATE ai.report_cache
           SET hit_count = hit_count + 1
         WHERE cache_key = $1
           AND expires_at > now()
        RETURNING content_md
    """
    async with leo_pool.acquire() as conn:
        row = await conn.fetchrow(sql, cache_key)
    return row["content_md"] if row else None


async def cache_set(
    leo_pool: asyncpg.Pool,
    cache_key: str,
    content_md: str,
    ttl_hours: int | None = None,
) -> None:
    """Записать в кеш с TTL."""
    ttl = ttl_hours or CACHE_TTL_HOURS
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl)
    sql = """
        INSERT INTO ai.report_cache (cache_key, content_md, expires_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (cache_key) DO UPDATE SET
            content_md = EXCLUDED.content_md,
            expires_at = EXCLUDED.expires_at,
            created_at = now(),
            hit_count = 0
    """
    async with leo_pool.acquire() as conn:
        await conn.execute(sql, cache_key, content_md, expires_at)


async def cache_cleanup_expired(leo_pool: asyncpg.Pool) -> int:
    """Удалить устаревшие записи. Возвращает количество удалённых."""
    sql = "DELETE FROM ai.report_cache WHERE expires_at <= now() RETURNING 1"
    async with leo_pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return len(rows)


# ----------------------------------------------------------------------------
# v1.7.1: История генераций
# ----------------------------------------------------------------------------
async def history_log(
    *,
    leo_pool: asyncpg.Pool,
    template_name: str,
    params: dict[str, Any],
    matrix_user_id: str,
    matrix_room_id: str,
    duration_ms: int,
    status: str,
    file_size: int | None = None,
    error_message: str | None = None,
    cache_hit: bool = False,
) -> int | None:
    """Записать в ai.report_history."""
    sql = """
        INSERT INTO ai.report_history (
            template_name, params_json, matrix_user_id, matrix_room_id,
            duration_ms, status, file_size, error_message, cache_hit
        ) VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
    """
    try:
        async with leo_pool.acquire() as conn:
            return await conn.fetchval(
                sql,
                template_name,
                json.dumps(params, ensure_ascii=False),
                matrix_user_id,
                matrix_room_id,
                duration_ms,
                status,
                file_size,
                error_message,
                cache_hit,
            )
    except Exception as e:
        log.warning("history_log failed: %s", e)
        return None


# ----------------------------------------------------------------------------
# GPT-4o-mini summarization
# ----------------------------------------------------------------------------
async def gpt_summarize(
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
) -> str:
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


# ----------------------------------------------------------------------------
# v1.7.1: Chart PNG (matplotlib)
# ----------------------------------------------------------------------------
def render_chart_png(
    *,
    title: str,
    labels: list[str],
    values: list[int],
    width_inches: float = 6.0,
    height_inches: float = 3.0,
    dpi: int = 110,
) -> bytes:
    """Сгенерировать bar-chart PNG.

    Возвращает bytes (можно сразу подставить в python-docx через io.BytesIO).
    Если matplotlib не установлен или рендер упал — возвращает b'' (вызывающий
    должен это уметь обработать).
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # без GUI
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib not available, skipping chart")
        return b""

    if not values or not labels or len(values) != len(labels):
        return b""

    try:
        fig, ax = plt.subplots(figsize=(width_inches, height_inches), dpi=dpi)
        bars = ax.bar(range(len(values)), values, color="#3a76c4", edgecolor="#1f4d8c")

        # Подписи значений над столбиками
        for bar, v in zip(bars, values):
            if v > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height(),
                    str(v),
                    ha="center", va="bottom",
                    fontsize=9, color="#333",
                )

        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=9)
        ax.set_title(title, fontsize=11, color="#222")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, axis="y", linestyle=":", alpha=0.5)
        ax.set_axisbelow(True)
        ax.margins(x=0.02)

        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning("chart render failed: %s", e)
        return b""
