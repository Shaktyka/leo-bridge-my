#!/usr/bin/env python3
"""
v1.7.0 patcher: расширяет _is_our_tool в letta_client.py
чтобы tools с префиксом leo_ распознавались как «наши» автоматически.

До патча: только leo_create_file явно в _OUR_TOOL_NAMES.
После: любой leo_* tool ловится по префиксу — leo_list_templates, leo_render_template и т.д.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/app/letta_client.py")
BACKUP = Path("/opt/ai/bridge/app/letta_client.py.bak-pre-v170")

# v1.6.0 версия (после нашего предыдущего патча)
OLD = '''        return (
            tool_name.startswith("calendar_")
            or tool_name.startswith("kb_")
            or tool_name.startswith("respect_")
            or tool_name in cls._OUR_TOOL_NAMES
        )'''

NEW = '''        return (
            tool_name.startswith("calendar_")
            or tool_name.startswith("kb_")
            or tool_name.startswith("respect_")
            or tool_name.startswith("leo_")
            or tool_name in cls._OUR_TOOL_NAMES
        )'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if 'tool_name.startswith("leo_")' in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: _is_our_tool marker not found in v1.6.0 form", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Run: sudo systemctl restart matrix-letta-bridge")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
