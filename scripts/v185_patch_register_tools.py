#!/usr/bin/env python3
"""
v1.8.5 patcher: усиливает первую строку docstring у calendar_list_events
чтобы Letta-агент чётко различал его и leo_render_template(calendar_summary).

Проблема (после v1.8.4):
- leo_render_template имеет 486-символьное description с "ВЫЗЫВАЙ для обзоров".
- calendar_list_events имеет 47-символьное "Получить список событий за период".
- LLM при запросе "сделай обзор моего календаря за прошлую неделю"
  всё равно выбирает calendar_list_events (короткое описание = универсальный tool).

Решение: переписать первую строку calendar_list_events так, чтобы:
- было ясно что он для "ПОКАЗАТЬ событий в чате текстом", а НЕ для обзоров/отчётов
- упомянуть leo_render_template как правильный выбор для обзоров

После применения нужно прогнать register_tools.py → описание обновится в Letta
автоматически через v1.8.4 механизм (DELETE+POST), и attach_tools_to_agents.py
переподключит к 41 агенту.

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/register_tools.py")
BACKUP = Path("/opt/ai/bridge/scripts/register_tools.py.bak-pre-v185")


OLD = '''    """
    Получить список событий пользователя за период.
    Args:
        matrix_room_id: ID Matrix-комнаты из контекста [matrix_room_id=...], начинается с "!".
        date_from_iso: Начало периода, ISO 8601 с TZ. Пример: "2026-04-26T00:00:00+03:00".
        date_to_iso: Конец периода, ISO 8601 с TZ.
        creator_user_id: MXID пользователя — ОБЯЗАТЕЛЬНО возьми из [from=...] в контексте.
            Например "@viacheslav:mtx.respectrb.ru". Без него события не найдутся.
    Returns:
        Текстовый список событий или сообщение что событий нет.
    Когда использовать:
        Когда пользователь спрашивает "что у нас на этой неделе", "какие встречи завтра",
        "покажи расписание", "покажи мой календарь".
    """'''

NEW = '''    """Текстовый список встреч из календаря в чат (НЕ docx-файл, НЕ отчёт). Используй для просмотровых запросов: "что у меня сегодня", "какие встречи завтра", "покажи расписание", "покажи календарь на эту неделю". НЕ ИСПОЛЬЗУЙ для запросов "сделай обзор/отчёт/ретроспективу" — для них есть leo_render_template(name="calendar_summary"), который создаёт docx-файл с LLM-обобщением, статистикой и графиком.

    Args:
        matrix_room_id: ID Matrix-комнаты из контекста [matrix_room_id=...], начинается с "!".
        date_from_iso: Начало периода, ISO 8601 с TZ. Пример: "2026-04-26T00:00:00+03:00".
        date_to_iso: Конец периода, ISO 8601 с TZ.
        creator_user_id: MXID пользователя — ОБЯЗАТЕЛЬНО возьми из [from=...] в контексте.
            Например "@viacheslav:mtx.respectrb.ru". Без него события не найдутся.

    Returns:
        Текстовый список событий или сообщение что событий нет.

    Когда использовать:
        - "что у нас на этой неделе" / "какие встречи завтра" / "покажи расписание" — да
        - "покажи мой календарь" / "что у меня в среду" — да
        - "сделай обзор календаря" / "отчёт по неделе" / "ретроспектива" — НЕТ,
          используй leo_render_template(name="calendar_summary") вместо этого.
    """'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "Текстовый список встреч из календаря в чат" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: calendar_list_events docstring not in expected form", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Now run scripts/register_tools.py — v1.8.4 will auto DELETE+POST")
    print("Then run scripts/attach_tools_to_agents.py to re-attach.")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
