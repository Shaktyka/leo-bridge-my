"""
Определение таймзоны для календарных операций в комнате.

Приоритеты:
1. Override в ai.room_calendar.timezone — задаётся админом
2. Профиль пользователя через MSC4175 (`us.cloke.msc4175.tz`)
3. None — пусть Leo спросит
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx


log = logging.getLogger("timezone_resolver")


VALID_TZ_PATTERN = ("/", "_")  # IANA TZ всегда содержит "/" или подчёркивания


def _looks_like_iana_tz(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    return "/" in s  # достаточно простая эвристика: Europe/Moscow, Asia/Tokyo


class TimezoneResolver:
    def __init__(
        self,
        pg_pool: asyncpg.Pool,
        matrix_homeserver: str,
        matrix_token: str,
    ) -> None:
        self.pg_pool = pg_pool
        self.matrix_homeserver = matrix_homeserver.rstrip("/")
        self.matrix_token = matrix_token
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TimezoneResolver":
        self._http = httpx.AsyncClient(
            timeout=10,
            headers={"Authorization": f"Bearer {self.matrix_token}"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def get_room_override(self, room_id: str) -> str | None:
        """Override от админа в БД."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT timezone FROM ai.room_calendar WHERE matrix_room_id = $1",
                room_id,
            )
            if row and row["timezone"]:
                tz = row["timezone"]
                if _looks_like_iana_tz(tz):
                    return tz
        return None

    async def get_user_tz_from_profile(self, user_id: str) -> str | None:
        """Читаем us.cloke.msc4175.tz из Matrix-профиля пользователя."""
        if self._http is None:
            return None
        url = f"{self.matrix_homeserver}/_matrix/client/v3/profile/{user_id}/us.cloke.msc4175.tz"
        try:
            r = await self._http.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
            tz = data.get("tz") or data.get("us.cloke.msc4175.tz")
            if isinstance(tz, str) and _looks_like_iana_tz(tz):
                return tz
        except Exception as e:
            log.debug("Failed to read MSC4175 tz for %s: %s", user_id, e)
        return None

    async def resolve(
        self, room_id: str, user_id: str | None = None
    ) -> str | None:
        """Вернуть TZ или None (Leo сам спросит)."""
        tz = await self.get_room_override(room_id)
        if tz:
            return tz
        if user_id:
            tz = await self.get_user_tz_from_profile(user_id)
            if tz:
                return tz
        return None

    async def set_room_override(self, room_id: str, tz: str) -> None:
        """Задать таймзону комнаты (вызывается из tool set_room_timezone)."""
        if not _looks_like_iana_tz(tz):
            raise ValueError(f"Not a valid IANA timezone: {tz}")
        async with self.pg_pool.acquire() as conn:
            # Если строки нет — создаём с пустым caldav_collection (заполнится при первом событии)
            await conn.execute(
                """
                INSERT INTO ai.room_calendar (matrix_room_id, caldav_collection, timezone)
                VALUES ($1, '', $2)
                ON CONFLICT (matrix_room_id)
                DO UPDATE SET timezone = EXCLUDED.timezone
                """,
                room_id,
                tz,
            )
            log.info("Room %s timezone set to %s", room_id, tz)
