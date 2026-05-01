"""
Аудит-лог взаимодействий с Letta.

Пишет каждое обращение в ai.ai_chat_log:
- кто спросил, в какой комнате
- что отправили в Letta, что вернулось
- latency, ошибки

Используется через async with AuditLog(dsn) as audit: await audit.log(...)
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg


log = logging.getLogger("audit")


class AuditLog:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def __aenter__(self) -> "AuditLog":
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=1,
            max_size=4,
            command_timeout=10,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def log(
        self,
        room_id: str,
        user_id: str,
        agent_id: str | None,
        user_message: str,
        assistant_answer: str | None,
        model_name: str,
        latency_ms: int,
        error_text: str | None = None,
    ) -> None:
        """
        Записать одно взаимодействие. Если БД недоступна — логируем
        предупреждение, но не падаем (аудит не должен ломать общение).
        """
        if self._pool is None:
            log.warning("AuditLog used outside context manager")
            return

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ai.ai_chat_log (
                        matrix_room_id,
                        matrix_user_id,
                        letta_agent_id,
                        user_message,
                        assistant_answer,
                        model_name,
                        latency_ms,
                        error_text
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    room_id,
                    user_id,
                    agent_id,
                    user_message,
                    assistant_answer,
                    model_name,
                    latency_ms,
                    error_text,
                )
        except Exception as e:
            log.error("Failed to write audit log: %s", e)
