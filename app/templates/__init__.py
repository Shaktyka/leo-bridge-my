"""
v1.8.1: Реестр шаблонов отчётов.

Изменения от v1.8.0:
- Усилен description для calendar_summary — явные триггер-фразы и упоминание docx
  чтобы LLM не путал с calendar_list_events.
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
    # v1.8.0 + усиленное v1.8.1 описание
    "calendar_summary": {
        "description": (
            "ИСПОЛЬЗУЙ ДЛЯ ЗАПРОСОВ типа 'сделай обзор календаря', "
            "'отчёт по календарю', 'как прошла неделя', "
            "'ретроспектива встреч', 'обзор недели по календарю'. "
            "Создаёт DOCX-ФАЙЛ с подробной ретроспективой за прошлые "
            "завершённые недели Mon-Sun: статистика (всего встреч, общее время), "
            "ASCII-чарт по дням, LLM-обобщение через GPT-4o-mini "
            "(главные темы, оценка загрузки), регулярные встречи, "
            "полный список по дням. В отличие от calendar_list_events возвращает "
            "именно структурированный документ-отчёт, а не текстовый список "
            "в чате. Параметр weeks_back: 1 = прошлая завершённая Mon-Sun, "
            "2 = последние 2 завершённые недели, и т.д. Текущая (ещё идущая) "
            "неделя НЕ включается."
        ),
        "params": {
            "weeks_back": {
                "type": "int",
                "default": 1,
                "description": "За сколько прошлых ЗАВЕРШЁННЫХ недель Mon-Sun показать обзор (1-8).",
            },
        },
        "renderer": calendar_summary.render,
    },
}


def list_templates() -> list[dict[str, Any]]:
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
    tmpl = TEMPLATES.get(name)
    if tmpl is None:
        raise ValueError(f"Unknown template: {name}. Available: {list(TEMPLATES.keys())}")

    renderer: Callable[..., Awaitable[dict[str, Any]]] = tmpl["renderer"]

    kwargs = {
        "params": params,
        "leo_pool": leo_pool,
        "matrix_room_id": matrix_room_id,
        "matrix_user_id": matrix_user_id,
    }

    sig = inspect.signature(renderer)
    if "cal_client" in sig.parameters:
        kwargs["cal_client"] = cal_client

    return await renderer(**kwargs)
