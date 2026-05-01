"""Обновить SYSTEM в Letta для всех существующих агентов на текущий из bridge.py."""
from __future__ import annotations

import os
import sys

import httpx

# Импорт SYSTEM_PROMPT из app.bridge
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.bridge import SYSTEM_PROMPT

LETTA_URL = os.environ["LETTA_URL"].rstrip("/")
LETTA_PASS = os.environ["LETTA_SERVER_PASSWORD"]


def main() -> int:
    headers = {"Authorization": f"Bearer {LETTA_PASS}"}
    with httpx.Client(timeout=30) as c:
        agents = c.get(f"{LETTA_URL}/v1/agents/", headers=headers).json()
        if not isinstance(agents, list):
            print("Failed to list agents:", agents)
            return 1

        print(f"Найдено агентов: {len(agents)}")
        for a in agents:
            agent_id = a["id"]
            name = a.get("name", "?")
            r = c.patch(
                f"{LETTA_URL}/v1/agents/{agent_id}",
                headers=headers,
                json={"system": SYSTEM_PROMPT},
            )
            if r.status_code >= 400:
                print(f"  {agent_id} ({name}): ERR {r.status_code}: {r.text[:120]}")
            else:
                print(f"  {agent_id} ({name}): SYSTEM обновлён")

    return 0


if __name__ == "__main__":
    sys.exit(main())

