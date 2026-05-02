#!/usr/bin/env python3
"""
v1.8.3 patcher: исправление как register_tools.py отправляет описание в Letta.

Проблема: при PATCH /v1/tools/{id} мы отправляли только source_code и
source_type. Letta обновляла source_code, но НЕ перегенерировала description
из нового docstring — оставляла исходный (как при первом CREATE).

Решение: явно вытащить первую строку (или первый абзац до пустой строки)
docstring через inspect.getdoc и положить в payload как 'description'.
Тогда Letta точно обновит description.

После применения нужно:
1. перезапустить scripts/register_tools.py — он отправит правильные description
2. убедиться что в Letta появились новые описания

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/register_tools.py")
BACKUP = Path("/opt/ai/bridge/scripts/register_tools.py.bak-pre-v183")

OLD = '''    for fn in TOOLS:
        name = fn.__name__
        source = inspect.getsource(fn)
        payload = {
            "source_code": source,
            "source_type": "python",
        }'''

NEW = '''    for fn in TOOLS:
        name = fn.__name__
        source = inspect.getsource(fn)
        # v1.8.3: явно отправляем description, иначе Letta не обновляет его
        # при PATCH — берёт первый абзац docstring как короткое описание
        doc = (inspect.getdoc(fn) or "").strip()
        # Берём всё до первого "\\n\\n" (один параграф) как description
        description = doc.split("\\n\\n", 1)[0].strip()
        payload = {
            "source_code": source,
            "source_type": "python",
            "description": description,
        }'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if 'v1.8.3: явно отправляем description' in text or '"description": description' in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: payload-construction block not found in expected form", file=sys.stderr)
        print("Expected to find this exact block:", file=sys.stderr)
        print(OLD, file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("CRITICAL: now run scripts/register_tools.py to push descriptions to Letta")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
