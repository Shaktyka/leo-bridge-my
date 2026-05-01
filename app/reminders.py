"""
Reminder-watcher как фоновая задача bridge.

Запускается из bridge.py параллельно с sync_forever.
Использует существующий matrix-nio AsyncClient (с теми же E2EE ключами),
поэтому напоминания приходят расшифрованными.

Логика:
- Раз в TICK_SEC секунд читает все Matrix-комнаты из bridge.sqlite
- Для каждой получает события из CalDAV на ближайшие LOOK_AHEAD_MIN
- Если до старта ≤ REMIND_BEFORE_MIN и ещё не уведомляли — шлёт E2EE-сообщение
- Дедуп через ai.notified_events
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

import asyncpg
from nio import AsyncClient

from app.calendar_client import CalendarClient


log = logging.getLogger("reminders")


# v1.1.0: REMIND_BEFORE_MIN — fallback для событий без VALARM
REMIND_BEFORE_MIN = 15
# Window для tick(). Должно быть >= max возможного reminder_minutes.
# 1440 минут = 24 часа. Покрывает "за день". Для "за неделю" нужно 10080.
LOOK_AHEAD_MIN = 10080
TICK_SEC = 60


class ReminderWatcher:
    def __init__(
        self,
        cal: CalendarClient,
        pg_pool: asyncpg.Pool,
        client: AsyncClient,
        bridge_sqlite_path: str,
    ) -> None:
        self.cal = cal
        self.pg_pool = pg_pool
        self.client = client
        self.sqlite_path = bridge_sqlite_path
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def _list_rooms(self) -> list[str]:
        """Все Matrix-комнаты, в которых работает Leo (из SQLite mapping)."""
        def _read() -> list[str]:
            conn = sqlite3.connect(self.sqlite_path)
            try:
                rows = conn.execute("SELECT room_id FROM room_agents").fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()
        return await asyncio.to_thread(_read)

    async def _is_notified(self, event_uid: str) -> bool:
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM ai.notified_events WHERE event_id = $1",
                event_uid,
            )
            return row is not None

    async def _mark_notified(self, event_uid: str, room_id: str) -> None:
        async with self.pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai.notified_events (event_id, room_id) "
                "VALUES ($1, $2) ON CONFLICT (event_id) DO NOTHING",
                event_uid,
                room_id,
            )

    async def _get_user_tz(self, user_id: str) -> str | None:
        """Прочитать TZ пользователя из Matrix-профиля (MSC4175)."""
        try:
            import httpx
            url = (
                f"{self.client.homeserver.rstrip('/')}/_matrix/client/v3/profile/"
                f"{user_id}/us.cloke.msc4175.tz"
            )
            token = self.client.access_token
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code != 200:
                    return None
                data = r.json()
                tz = data.get("tz") or data.get("us.cloke.msc4175.tz")
                if isinstance(tz, str) and "/" in tz:
                    return tz
        except Exception as e:
            log.debug("Failed to read user TZ for %s: %s", user_id, e)
        return None

    async def _get_room_tz(self, room_id: str) -> str | None:
        """Достать TZ комнаты из ai.room_calendar (override от админа/пользователя)."""
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT timezone FROM ai.room_calendar WHERE matrix_room_id = $1",
                room_id,
            )
            if row and row["timezone"]:
                return row["timezone"]
        return None

    @staticmethod
    def _format_reminder(
        title: str,
        start: datetime,
        location: str | None,
        creator_tz: str | None,
    ) -> str:
        """
        Текст напоминания со временем в TZ создателя события.
        Если TZ создателя неизвестна — показываем UTC.
        """
        from zoneinfo import ZoneInfo
        if creator_tz:
            try:
                local_start = start.astimezone(ZoneInfo(creator_tz))
                when = local_start.strftime("%H:%M")
                tz_label = f" ({creator_tz})"
            except Exception:
                local_start = start.astimezone(ZoneInfo("UTC"))
                when = local_start.strftime("%H:%M")
                tz_label = " UTC"
        else:
            local_start = start.astimezone(ZoneInfo("UTC"))
            when = local_start.strftime("%H:%M")
            tz_label = " UTC"

        parts = [f"⏰ Напоминание: **{title}** в {when}{tz_label}"]
        if location:
            parts.append(f"📍 {location}")
        return "\n".join(parts)

    async def _send_to_room(self, room_id: str, text: str) -> bool:
        """E2EE-безопасная отправка через client bridge'а."""
        try:

            # Конвертируем markdown → HTML
            from markdown_it import MarkdownIt
            md = MarkdownIt("commonmark", {"breaks": True, "html": False, "linkify": True})
            html = md.render(text)

            room = self.client.rooms.get(room_id)
            if room is None:
                log.warning("Room %s not in client.rooms; skip", room_id)
                return False

            # Если комната E2EE — заранее раздать megolm-ключ всем участникам.
            # Без этого Element получит зашифрованный пакет, но не сможет расшифровать.
            if getattr(room, "encrypted", False):
                try:
                    if self.client.olm:
                        await self.client.share_group_session(
                            room_id,
                            ignore_unverified_devices=True,
                        )
                        log.info("Shared megolm session for %s", room_id[:20])
                        # Дать ключам время дойти до получателей
                        # перед отправкой зашифрованного сообщения
                        await asyncio.sleep(2.0)
                except Exception as e:
                    log.warning("share_group_session for %s: %s", room_id[:20], e)

            log.info(
                "Sending reminder to %s (encrypted=%s, members=%d)",
                room_id[:20],
                getattr(room, "encrypted", "?"),
                len(room.users),
            )

            response = await self.client.room_send(
                room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": text,
                    "format": "org.matrix.custom.html",
                    "formatted_body": html,
                },
                ignore_unverified_devices=True,
            )

            if hasattr(response, "event_id") and response.event_id:
                log.info("Reminder sent OK, event_id=%s", response.event_id)
                return True
            log.error("room_send returned non-event response: %s", response)
            return False
        except Exception as e:
            log.error("Failed to send reminder to %s: %s", room_id, e)
            return False

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        window_start = now
        window_end = now + timedelta(minutes=LOOK_AHEAD_MIN)

        # v1.3.0: читаем все ICS напрямую с ФС — один вызов вместо N per-room
        data_root = Path(os.environ.get(
            "RADICALE_DATA",
            "/opt/ai/radicale/data/collections/collection-root"
        ))
        events = await asyncio.to_thread(
            self._scan_ics_files, data_root, window_start, window_end
        )
        if not events:
            return

        for ev in events:
            seconds_until = (ev.start - now).total_seconds()
            if seconds_until < 0:
                continue
            rm = getattr(ev, "reminder_minutes", None)
            if rm is None:
                threshold_sec = REMIND_BEFORE_MIN * 60
            elif rm <= 0:
                continue
            else:
                threshold_sec = rm * 60
            if seconds_until > threshold_sec:
                continue
            if await self._is_notified(ev.uid):
                continue

            # Определяем комнату для отправки — из notified_events или room_agents
            room_id = await self._find_room_for_event(ev)
            if not room_id:
                log.debug("No room for event %s creator=%s", ev.uid[:8], ev.creator_user_id)
                continue

            creator_tz = None
            if ev.creator_user_id:
                creator_tz = await self._get_user_tz(ev.creator_user_id)
            text = self._format_reminder(ev.title, ev.start, ev.location, creator_tz)
            ok = await self._send_to_room(room_id, text)
            if ok:
                await self._mark_notified(ev.uid, room_id)
                log.info("Reminded %s about %s (in %ds)",
                         room_id[:20], ev.title[:30], int(seconds_until))

    def _scan_ics_files(
        self,
        data_root: Path,
        window_start: datetime,
        window_end: datetime,
    ) -> list:
        """Читаем все ICS напрямую с файловой системы."""
        results = []
        try:
            ics_files = list(data_root.rglob("*.ics"))
        except Exception as e:
            log.error("Failed to scan %s: %s", data_root, e)
            return results

        from icalendar import Calendar as iCal
        for ics_path in ics_files:
            if ".Radicale.cache" in str(ics_path):
                continue
            try:
                raw = ics_path.read_bytes()
                cal_data = iCal.from_ical(raw)
                for comp in cal_data.walk("VEVENT"):
                    dto = self.cal._dto_from_ical(comp)
                    if dto is None:
                        continue
                    if dto.end < window_start:
                        continue
                    if dto.start > window_end:
                        continue
                    results.append(dto)
            except Exception as e:
                log.debug("Parse error %s: %s", ics_path.name, e)
        log.info("Scanned %d ICS files, found %d events in window",
                  len(ics_files), len(results))
        return results

    async def _find_room_for_event(self, ev) -> str | None:
        """Находим комнату для отправки напоминания.
        
        Стратегия:
        1. Уже уведомляли → в ту же комнату (notified_events)
        2. creator_user_id → свежая комната из ai.user_rooms
        3. Fallback: первая комната из room_agents
        """
        # 1. Уже уведомляли — в ту же комнату
        try:
            async with self.pg_pool.acquire() as conn:
                row = await conn.fetchval(
                    "SELECT room_id FROM ai.notified_events WHERE event_id = $1 LIMIT 1",
                    ev.uid,
                )
                if row:
                    return row
        except Exception:
            pass

        # 2. Свежая комната пользователя из ai.user_rooms
        creator = ev.creator_user_id or ""
        if creator:
            try:
                async with self.pg_pool.acquire() as conn:
                    row = await conn.fetchval(
                        """
                        SELECT matrix_room_id FROM ai.user_rooms
                        WHERE matrix_user_id = $1
                        ORDER BY last_seen_at DESC
                        LIMIT 1
                        """,
                        creator,
                    )
                    if row:
                        return row
            except Exception as e:
                log.debug("user_rooms lookup failed: %s", e)

        # 3. Fallback: первая комната из SQLite room_agents
        try:
            def _fallback():
                import sqlite3 as _sq
                conn = _sq.connect(self.sqlite_path)
                try:
                    row = conn.execute(
                        "SELECT room_id FROM room_agents ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                    return row[0] if row else None
                finally:
                    conn.close()
            return await asyncio.to_thread(_fallback)
        except Exception as e:
            log.error("_find_room_for_event fallback failed: %s", e)
            return None

    async def run_forever(self) -> None:
        log.info(
            "ReminderWatcher started: tick=%ds, remind_before=%dmin",
            TICK_SEC,
            REMIND_BEFORE_MIN,
        )
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                log.exception("Tick failed")
            try:
                await asyncio.sleep(TICK_SEC)
            except asyncio.CancelledError:
                break
        log.info("ReminderWatcher stopped")
