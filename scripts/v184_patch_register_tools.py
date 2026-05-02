#!/usr/bin/env python3
"""
v1.8.4 patcher: чинит register_tools.py чтобы он:
1. Явно отправлял description в payload (фикс который v1.8.3 пытался сделать)
2. Автоматически делал DELETE+POST когда description в Letta отличается
   от локального (потому что Letta PATCH не обновляет description)

Идемпотентен. Применяется к v1.7.0-baseline формату register_tools.py.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/register_tools.py")
BACKUP = Path("/opt/ai/bridge/scripts/register_tools.py.bak-pre-v184")


# Текущий код (v1.7.0 baseline) — без description в payload, простой PATCH
OLD = '''    for fn in TOOLS:
        name = fn.__name__
        source = inspect.getsource(fn)
        payload = {
            "source_code": source,
            "source_type": "python",
        }

        if name in existing:
            tool_id = existing[name]
            r = httpx.patch(
                f"{LETTA_URL}/v1/tools/{tool_id}",
                headers=headers,
                json=payload,
                timeout=30,
            )
            print(f"Updated  {name:30s} -> {tool_id} [{r.status_code}]")
            if r.status_code >= 400:
                print(f"   Body: {r.text[:300]}")
                sys.exit(1)
        else:
            r = httpx.post(
                f"{LETTA_URL}/v1/tools/",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if r.status_code >= 400:
                print(f"FAILED   {name}: {r.status_code} {r.text[:500]}")
                sys.exit(1)'''

# v1.8.4: новый блок — с description, с auto DELETE+POST когда description расходится
NEW = '''    # v1.8.4: подгружаем существующие tool'ы целиком (с description),
    # чтобы понимать когда description изменился и нужен DELETE+POST вместо PATCH
    r2 = httpx.get(f"{LETTA_URL}/v1/tools/", headers=headers, timeout=30)
    r2.raise_for_status()
    existing_full = {t["name"]: t for t in r2.json()}

    for fn in TOOLS:
        name = fn.__name__
        source = inspect.getsource(fn)
        # v1.8.4: явно достаём description из docstring (первый абзац до пустой строки).
        # Letta берёт это как короткое описание tool'а, которое видит LLM-агент.
        doc = (inspect.getdoc(fn) or "").strip()
        description = doc.split("\\n\\n", 1)[0].strip()
        payload = {
            "source_code": source,
            "source_type": "python",
            "description": description,
        }

        if name in existing:
            tool_id = existing[name]
            # v1.8.4: проверяем — изменилось ли description.
            # Letta PATCH игнорирует description; если другой — делаем DELETE+POST.
            current_desc = (existing_full.get(name, {}).get("description") or "").strip()
            if current_desc != description:
                print(f"Recreate {name:30s} -> description changed, DELETE+POST")
                r = httpx.delete(
                    f"{LETTA_URL}/v1/tools/{tool_id}",
                    headers=headers,
                    timeout=30,
                )
                if r.status_code >= 400:
                    print(f"   DELETE failed: {r.status_code} {r.text[:300]}")
                    sys.exit(1)
                r = httpx.post(
                    f"{LETTA_URL}/v1/tools/",
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
                if r.status_code >= 400:
                    print(f"   POST failed: {r.status_code} {r.text[:500]}")
                    sys.exit(1)
                new_id = r.json().get("id")
                print(f"  -> new tool_id: {new_id}")
                print(f"  -> RUN attach_tools_to_agents.py to re-attach!")
            else:
                r = httpx.patch(
                    f"{LETTA_URL}/v1/tools/{tool_id}",
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
                print(f"Updated  {name:30s} -> {tool_id} [{r.status_code}]")
                if r.status_code >= 400:
                    print(f"   Body: {r.text[:300]}")
                    sys.exit(1)
        else:
            r = httpx.post(
                f"{LETTA_URL}/v1/tools/",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if r.status_code >= 400:
                print(f"FAILED   {name}: {r.status_code} {r.text[:500]}")
                sys.exit(1)
            print(f"Created  {name:30s} -> {r.json().get('id')}")'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "v1.8.4" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: register_tools.py loop not found in expected (v1.7.0) form", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Now register_tools.py auto DELETE+POST when description changes.")
    print("After running register_tools.py, ALWAYS run attach_tools_to_agents.py.")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
