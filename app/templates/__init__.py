"""
v1.7.0: Реестр шаблонов отчётов.

Каждый шаблон — это (description, params_schema, renderer).
Renderer — async-функция, которая:
  1. Принимает params, leo_pool, matrix_room_id, matrix_user_id
  2. Идёт в БД и/или в LLM
  3. Возвращает {filename, format, content_md, title} — готовый для leo_create_file

Регистрация нового шаблона: добавь импорт + запись в TEMPLATES.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import asyncpg

from app.templates import (
    competitor_summary,
    kb_changes_digest,
    topic_compendium,
    weekly_infopovody,
)


# (description, params_schema, renderer)
# params_schema — словарь с описанием параметров для LLM
TEMPLATES: dict[str, dict[str, Any]] = {
    "weekly_infopovody": {
        "description": (
            "Еженедельный обзор инфоповодов: все карточки из раздела ИНФОПОВОДЫ "
            "за последние N недель, сгруппированные по дате актуализации."
        ),
        "params": {
            "weeks_back": {
                "type": "int",
                "default": 1,
                "description": "За сколько прошлых недель брать инфоповоды (1-12).",
            },
        },
        "renderer": weekly_infopovody.render,
    },
    "kb_changes_digest": {
        "description": (
            "Дайджест изменений в корпоративной KB за период: что обновилось "
            "за последние N дней (по полю updated_at источника)."
        ),
        "params": {
            "days_back": {
                "type": "int",
                "default": 7,
                "description": "За сколько прошлых дней показывать изменения (1-90).",
            },
        },
        "renderer": kb_changes_digest.render,
    },
    "competitor_summary": {
        "description": (
            "Сводка по конкуренту с использованием LLM: находит до N материалов "
            "про указанного конкурента и формирует краткие тезисы по теме."
        ),
        "params": {
            "competitor": {
                "type": "str",
                "description": "Название конкурента: Гарант, Актион, КонсультантПлюс, и т.п.",
            },
            "limit": {
                "type": "int",
                "default": 15,
                "description": "Сколько карточек анализировать (1-50). Больше = глубже но дороже.",
            },
        },
        "renderer": competitor_summary.render,
    },
    "topic_compendium": {
        "description": (
            "Подборка по теме: всё что есть в KB по указанной теме, "
            "сгруппированное по разделам со ссылками на материалы."
        ),
        "params": {
            "topic": {
                "type": "str",
                "description": "Тема для поиска (на русском). Пример: 'командировки', 'отпуска', 'НДФЛ'.",
            },
            "limit": {
                "type": "int",
                "default": 20,
                "description": "Сколько материалов в подборке (1-50).",
            },
        },
        "renderer": topic_compendium.render,
    },
}


def list_templates() -> list[dict[str, Any]]:
    """Список шаблонов для LLM (через tool leo_list_templates)."""
    return [
        {
            "name": name,
            "description": t["description"],
            "params": t["params"],
        }
        for name, t in TEMPLATES.items()
    ]


async def render(
    *,
    name: str,
    params: dict[str, Any],
    leo_pool: asyncpg.Pool,
    matrix_room_id: str,
    matrix_user_id: str,
) -> dict[str, Any]:
    """Запустить рендер шаблона по имени.

    Returns:
        {filename, format, content_md, title} — готовое для leo_create_file
    """
    tmpl = TEMPLATES.get(name)
    if tmpl is None:
        raise ValueError(f"Unknown template: {name}. Available: {list(TEMPLATES.keys())}")

    renderer: Callable[..., Awaitable[dict[str, Any]]] = tmpl["renderer"]
    return await renderer(
        params=params,
        leo_pool=leo_pool,
        matrix_room_id=matrix_room_id,
        matrix_user_id=matrix_user_id,
    )
