"""
SQLite mapping room_id -> letta_agent_id.

Возвращаем (agent_id, _) для совместимости с предыдущей версией кода.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiosqlite


log = logging.getLogger("room_mapping")


SCHEMA = """
CREATE TABLE IF NOT EXISTS room_agents (
    room_id    TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class RoomMapping:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "RoomMapping":
        self._conn = await aiosqlite.connect(str(self.db_path))
        await self._conn.execute(SCHEMA)
        await self._conn.commit()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("RoomMapping must be used inside `async with`")
        return self._conn

    async def get_state(self, room_id: str) -> tuple[str | None, None]:
        """Возвращает (agent_id, None) — второй элемент для совместимости."""
        async with self.conn.execute(
            "SELECT agent_id FROM room_agents WHERE room_id = ?",
            (room_id,),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None, None
            return row[0], None

    async def set_agent(self, room_id: str, agent_id: str) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO room_agents (room_id, agent_id) VALUES (?, ?) "
                "ON CONFLICT(room_id) DO UPDATE SET agent_id = excluded.agent_id",
                (room_id, agent_id),
            )
            await self.conn.commit()
            log.info("Mapped %s -> %s", room_id, agent_id)

    async def delete_room(self, room_id: str) -> None:
        """v1.4.1: удалить запись о комнате (cleanup пустых комнат)."""
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM room_agents WHERE room_id = ?",
                (room_id,),
            )
            await self.conn.commit()
            log.info("Deleted room mapping %s", room_id)
