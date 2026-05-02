"""
v1.8.0: Реестр шаблонов отчётов.

Изменения от v1.7.0:
- Добавлен calendar_summary в TEMPLATES.
- render() теперь принимает опциональный cal_client (CalendarClient) и
  пробрасывает его рендерерам, которым он нужен.

Каждый шаблон — это (description, params_schema, renderer).
Renderer — async-функция, которая принимает (params, leo_pool, matrix_room_id,
matrix_user_id, и опционально cal_client).
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

import asyncpg

from app.templates import (
    calendar_summary,
    competitor_summary,
    kb_changes_digest,
    topic_compendium,
    weekly_infopovody,
)


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
            "про указанного конкурента и формирует краткие тезисы по теме. "
            "Кешируется на 24ч."
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
    # v1.8.0
    "calendar_summary": {
        "description": (
            "Ретроспектива по личному календарю за прошлые N недель: "
            "статистика, повторяющиеся встречи, LLM-обзор главных тем недели."
        ),
        "params": {
            "weeks_back": {
                "type": "int",
                "default": 1,
                "description": "За сколько прошлых недель показать обзор (1-8).",
            },
        },
        "renderer": calendar_summary.render,
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
    cal_client: Any = None,
) -> dict[str, Any]:
    """Запустить рендер шаблона по имени.

    v1.8.0: автодетект сигнатуры рендерера. Шаблонам которым нужен cal_client
    (calendar_summary), он пробрасывается; остальные (KB-шаблоны) получают
    только базовый набор аргументов.
    """
    tmpl = TEMPLATES.get(name)
    if tmpl is None:
        raise ValueError(f"Unknown template: {name}. Available: {list(TEMPLATES.keys())}")

    renderer: Callable[..., Awaitable[dict[str, Any]]] = tmpl["renderer"]

    # Базовый набор аргументов
    kwargs = {
        "params": params,
        "leo_pool": leo_pool,
        "matrix_room_id": matrix_room_id,
        "matrix_user_id": matrix_user_id,
    }

    # Если рендерер принимает cal_client — пробрасываем
    sig = inspect.signature(renderer)
    if "cal_client" in sig.parameters:
        kwargs["cal_client"] = cal_client

    return await renderer(**kwargs)
