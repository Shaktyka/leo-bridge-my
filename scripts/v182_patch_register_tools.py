#!/usr/bin/env python3
"""
v1.8.2 patcher: критичный фикс — переписывает ПЕРВУЮ СТРОКУ docstring'а
функции leo_render_template в register_tools.py.

Зачем: Letta читает только первую строку docstring как description tool'а
(а не весь docstring). В v1.8.1 мы добавили case "calendar_summary" в блок
'Когда использовать', но Letta его игнорирует, потому что первая строка
по-прежнему "Сгенерировать готовый отчёт по шаблону на основе корпоративной
KB Респект.Чата и отправить в Matrix-чат как docx-файл."

После этого patcher'а первая строка станет агрессивнее и упомянет триггер-фразы.

Нужно: после применения прогнать scripts/register_tools.py чтобы Letta
получила новую description.

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/register_tools.py")
BACKUP = Path("/opt/ai/bridge/scripts/register_tools.py.bak-pre-v182")


# Старая первая строка docstring (что есть сейчас)
OLD = '''def leo_render_template(
    name: str,
    matrix_room_id: str,
    matrix_user_id: str,
    weeks_back: int = 0,
    days_back: int = 0,
    competitor: str = "",
    topic: str = "",
    limit: int = 0,
) -> str:
    """
    Сгенерировать готовый отчёт по шаблону на основе корпоративной KB Респект.Чата
    и отправить в Matrix-чат как docx-файл.'''

# Новая первая строка — короче и агрессивнее, с триггер-фразами в начале
NEW = '''def leo_render_template(
    name: str,
    matrix_room_id: str,
    matrix_user_id: str,
    weeks_back: int = 0,
    days_back: int = 0,
    competitor: str = "",
    topic: str = "",
    limit: int = 0,
) -> str:
    """Создать docx-отчёт по шаблону. ВЫЗЫВАЙ для "сделай обзор/отчёт/ретроспективу/сводку/подборку/дайджест": еженедельный обзор инфоповодов (weekly_infopovody), изменений в KB (kb_changes_digest), сводки по конкуренту (competitor_summary), подборки по теме (topic_compendium), обзора календаря за прошлую неделю (calendar_summary). Возвращает готовый файл в чат, не текстовый ответ. Для календаря ИСПОЛЬЗУЙ это вместо calendar_list_events когда юзер просит "обзор", "отчёт", "ретроспективу".'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "Создать docx-отчёт по шаблону. ВЫЗЫВАЙ" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print(
            "ERROR: leo_render_template definition not found in v1.7.0 form. "
            "Возможно, файл уже был изменён вручную.", file=sys.stderr
        )
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("CRITICAL: run scripts/register_tools.py NOW to push new description to Letta")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
