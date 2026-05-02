#!/usr/bin/env python3
"""
v1.8.1 patcher: усиливает docstring tool'а leo_render_template в register_tools.py
- добавляет явный пример для calendar_summary с триггер-фразами
- предупреждает что для календаря НЕ нужно использовать calendar_list_events
  если юзер просит "обзор" / "отчёт" / "файл"

После применения нужно прогнать register_tools.py чтобы Letta получила
обновлённое описание tool'а.

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/register_tools.py")
BACKUP = Path("/opt/ai/bridge/scripts/register_tools.py.bak-pre-v181")


# Старый блок "Когда использовать" из v1.7.0
OLD = '''    Когда использовать:
        - Пользователь просит "сделай еженедельный обзор инфоповодов" → weekly_infopovody
        - "Что у нас обновилось в KB за последнюю неделю" → kb_changes_digest
        - "Сделай сводку по Гаранту" / "что есть про КонсультантПлюс" → competitor_summary
        - "Подбери всё про командировки" / "сделай подборку по НДФЛ" → topic_compendium

    Когда НЕ использовать:
        - Простой поисковый запрос — используй respect_kb_search вместо этого
        - Если шаблон не подходит под запрос пользователя — лучше respect_kb_search
        - Если пользователь хочет сам читать ответ в чате (не файлом) — respect_kb_search
    """'''

NEW = '''    Когда использовать:
        - Пользователь просит "сделай еженедельный обзор инфоповодов" → weekly_infopovody
        - "Что у нас обновилось в KB за последнюю неделю" → kb_changes_digest
        - "Сделай сводку по Гаранту" / "что есть про КонсультантПлюс" → competitor_summary
        - "Подбери всё про командировки" / "сделай подборку по НДФЛ" → topic_compendium
        - "Сделай обзор моего календаря" / "отчёт по календарю" / "как прошла неделя"
          / "ретроспектива встреч" → calendar_summary (создаёт docx-файл!).
          Это ПРИОРИТЕТНЫЙ выбор для запросов про "обзор/отчёт/анализ" календаря —
          НЕ используй calendar_list_events для таких запросов, calendar_list_events
          только для простого "покажи встречи / какие встречи на той неделе".

    Когда НЕ использовать:
        - Простой поисковый запрос — используй respect_kb_search вместо этого
        - Если шаблон не подходит под запрос пользователя — лучше respect_kb_search
        - Если пользователь хочет сам читать ответ в чате (не файлом) — respect_kb_search
        - Простой просмотр встреч "что у меня сегодня" → calendar_list_events
    """'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "calendar_summary (создаёт docx-файл!)" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print(
            "ERROR: 'Когда использовать' block not found in v1.7.0 form. "
            "Возможно файл изменён вручную — проверь.", file=sys.stderr
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
    print("Next: run scripts/register_tools.py to push updated descriptions to Letta")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
