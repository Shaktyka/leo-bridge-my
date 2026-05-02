#!/usr/bin/env python3
"""
v1.8.0 patcher: модифицирует /templates/render endpoint в internal_api.py.

Изменение: передавать state.cal (CalendarClient) в render_template как параметр
cal_client. Нужно для нового шаблона calendar_summary.

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/app/internal_api.py")
BACKUP = Path("/opt/ai/bridge/app/internal_api.py.bak-pre-v180")

# Состояние после v1.7.1 patch
OLD = '''            rendered = await render_template(
                name=req.name,
                params=req.params,
                leo_pool=state.pg_pool,
                matrix_room_id=req.matrix_room_id,
                matrix_user_id=req.matrix_user_id,
            )'''

NEW = '''            rendered = await render_template(
                name=req.name,
                params=req.params,
                leo_pool=state.pg_pool,
                matrix_room_id=req.matrix_room_id,
                matrix_user_id=req.matrix_user_id,
                cal_client=getattr(state, "cal", None),  # v1.8.0
            )'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "cal_client=getattr(state" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: render_template call (v1.7.1 form) not found", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Run: sudo systemctl restart bridge-internal-api")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
