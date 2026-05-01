"""
Тонкая обёртка над caldav-библиотекой для работы с Radicale.

Поддерживает:
- Lazy-создание коллекции на комнату
- Создание события с RRULE и DTSTART/DTEND в указанной TZ
- Список событий за период
- Поиск по тексту
- Удаление события по uid
- Обновление события

Для thread-safety: caldav синхронный — все операции выполняются через asyncio.to_thread().
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import caldav
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event


log = logging.getLogger("calendar_client")


@dataclass
class EventDTO:
    """Удобное представление события для отдачи в Letta."""
    uid: str
    title: str
    description: str | None
    start: datetime
    end: datetime
    timezone: str
    location: str | None = None
    creator_user_id: str | None = None
    reminder_minutes: int | None = 15

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "timezone": self.timezone,
            "location": self.location,
            "creator_user_id": self.creator_user_id,
            "reminder_minutes": self.reminder_minutes,
        }


class CalendarClient:
    def __init__(self, url: str, username: str, password: str) -> None:
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        # caldav.DAVClient — синхронный. Создаём в _ensure_connection.
        self._client: caldav.DAVClient | None = None
        self._principal: caldav.Principal | None = None

    # ----- внутренняя инфра -----
    def _ensure_sync(self) -> None:
        if self._client is None:
            self._client = caldav.DAVClient(
                url=self.url,
                username=self.username,
                password=self.password,
            )
            self._principal = self._client.principal()

    @staticmethod
    def _safe_collection_name(matrix_room_id: str) -> str:
        """Из !MnRzxVDy:mtx.respectrb.ru делаем room_MnRzxVDy. Legacy."""
        body = matrix_room_id.lstrip("!").split(":", 1)[0]
        body = re.sub(r"[^A-Za-z0-9_-]", "", body)[:24]
        return f"room_{body}"

    @staticmethod
    def _user_collection_name(creator_user_id: str | None) -> str:
        """Из @viacheslav:mtx.respectrb.ru → ('viacheslav', 'leo').
        Возвращает (username, cal_id) для коллекции пользователя."""
        if not creator_user_id:
            return "assistant", "leo"
        # @username:homeserver → username
        username = creator_user_id.lstrip("@").split(":", 1)[0]
        username = re.sub(r"[^A-Za-z0-9_-]", "", username)[:32] or "user"
        return username, "leo"

    def _get_or_create_collection_sync(
        self, matrix_room_id: str, display_name: str,
        creator_user_id: str | None = None,
    ) -> caldav.Calendar:
        self._ensure_sync()
        assert self._principal is not None
        # v1.3.0: коллекция per-user вместо per-room
        username, cal_id = self._user_collection_name(creator_user_id)
        url = f"{self.url}/{username}/{cal_id}/"
        try:
            cal = self._principal.calendar(cal_url=url)
            _ = cal.get_properties()
            log.info("Using collection url=%s", cal.url)
            return cal
        except (NotFoundError, Exception) as _col_err:
            log.info("Collection %s/%s not found, creating via MKCALENDAR...",
                     username, cal_id)
            # Создаём коллекцию через прямой MKCALENDAR запрос под нужным пользователем
            # Radicale с type=authenticated разрешает это
            import requests as _req
            mkcal_url = f"{self.url}/{username}/{cal_id}/"
            mkcal_body = """<?xml version="1.0" encoding="UTF-8"?>
<mkcalendar xmlns="urn:ietf:params:xml:ns:caldav">
  <set><prop>
    <displayname>Leo Calendar</displayname>
    <calendar-color>#2E5FA3</calendar-color>
  </prop></set>
</mkcalendar>"""
            try:
                resp = _req.request(
                    "MKCALENDAR",
                    mkcal_url,
                    data=mkcal_body,
                    headers={"Content-Type": "application/xml; charset=utf-8"},
                    auth=(self.username, self.password),
                    timeout=10,
                )
                log.info("MKCALENDAR %s → %s", mkcal_url, resp.status_code)
            except Exception as _me:
                log.warning("MKCALENDAR failed: %s", _me)

            # Теперь открываем созданную коллекцию
            try:
                cal = self._principal.calendar(cal_url=mkcal_url)
                _ = cal.get_properties()
                log.info("Collection %s ready", mkcal_url)
                return cal
            except Exception as _e2:
                log.warning("Still can't open %s: %s — fallback to assistant/leo", mkcal_url, _e2)
                fallback_url = f"{self.url}/{self.username}/leo/"
                try:
                    cal = self._principal.calendar(cal_url=fallback_url)
                    _ = cal.get_properties()
                    return cal
                except Exception:
                    return self._principal.make_calendar(
                        name="Leo",
                        cal_id="leo",
                    )

    # ----- публичные методы (async) -----
    async def ensure_collection(
        self, matrix_room_id: str, display_name: str = ""
    ) -> str:
        """Гарантирует, что коллекция существует. Возвращает имя коллекции."""
        await asyncio.to_thread(
            self._get_or_create_collection_sync, matrix_room_id, display_name
        )
        return self._safe_collection_name(matrix_room_id)

    async def create_event(
        self,
        matrix_room_id: str,
        room_display_name: str,
        title: str,
        start: datetime,
        end: datetime,
        timezone: str,
        description: str | None = None,
        location: str | None = None,
        creator_user_id: str | None = None,
        reminder_minutes: int | None = 15,
    ) -> EventDTO:
        """Создать событие. start/end должны быть aware datetime в указанной TZ."""
        from datetime import timezone as _tz

        # Принудительно конвертируем в UTC + naive для записи в Zulu format
        if start.tzinfo is None:
            start_utc = start.replace(tzinfo=_tz.utc)
        else:
            start_utc = start.astimezone(_tz.utc)
        if end.tzinfo is None:
            end_utc = end.replace(tzinfo=_tz.utc)
        else:
            end_utc = end.astimezone(_tz.utc)

        def _sync() -> EventDTO:
            cal = self._get_or_create_collection_sync(matrix_room_id, room_display_name, creator_user_id)
            uid = str(uuid.uuid4())

            # Форматирование Zulu: 20260425T143400Z
            def fmt_z(d: datetime) -> str:
                return d.strftime("%Y%m%dT%H%M%SZ")

            now_z = fmt_z(datetime.now(_tz.utc))

            # Экранирование для iCal: запятые, точки с запятой, переводы строк
            def esc(s: str) -> str:
                return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

            lines = [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Leo AI Assistant//mtx.respectrb.ru//RU",
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now_z}",
                f"DTSTART:{fmt_z(start_utc)}",
                f"DTEND:{fmt_z(end_utc)}",
                f"SUMMARY:{esc(title)}",
            ]
            if creator_user_id:
                # X-LEO-CREATOR — кастомное поле для watcher,
                # чтобы знать чью TZ использовать в напоминании
                lines.append(f"X-LEO-CREATOR:{esc(creator_user_id)}")
            if description:
                lines.append(f"DESCRIPTION:{esc(description)}")
            if location:
                lines.append(f"LOCATION:{esc(location)}")
            # VALARM для настраиваемых напоминаний (v1.1.0)
            if reminder_minutes is not None and reminder_minutes > 0:
                # ISO 8601 duration: PT15M, PT60M, P1D
                if reminder_minutes < 60:
                    trigger = f"PT{reminder_minutes}M"
                elif reminder_minutes < 1440:  # < 24 hours
                    hours = reminder_minutes // 60
                    mins = reminder_minutes % 60
                    if mins == 0:
                        trigger = f"PT{hours}H"
                    else:
                        trigger = f"PT{hours}H{mins}M"
                else:  # days
                    days = reminder_minutes // 1440
                    remain = reminder_minutes % 1440
                    if remain == 0:
                        trigger = f"P{days}D"
                    else:
                        hours = remain // 60
                        mins = remain % 60
                        if mins == 0:
                            trigger = f"P{days}DT{hours}H"
                        else:
                            trigger = f"P{days}DT{hours}H{mins}M"
                
                lines.extend([
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY", 
                    f"TRIGGER:-{trigger}",
                    "DESCRIPTION:Leo reminder",
                    "END:VALARM"
                ])
            lines.extend(["END:VEVENT", "END:VCALENDAR"])

            ics_text = "\r\n".join(lines) + "\r\n"
            cal.save_event(ics_text)

            return EventDTO(
                uid=uid,
                title=title,
                description=description,
                start=start_utc,
                end=end_utc,
                timezone=timezone,
                location=location,
                creator_user_id=creator_user_id,
                reminder_minutes=reminder_minutes,
            )

        return await asyncio.to_thread(_sync)

    async def list_events(
        self,
        matrix_room_id: str,
        room_display_name: str,
        date_from: datetime,
        date_to: datetime,
        creator_user_id: str | None = None,
    ) -> list[EventDTO]:
        """Список событий за период.

        Используем cal.events() (все события коллекции) с локальной фильтрацией —
        cal.search() в caldav-lib не всегда находит свежесозданные события Radicale.
        """
        def _sync() -> list[EventDTO]:
            cal = self._get_or_create_collection_sync(matrix_room_id, room_display_name, creator_user_id)
            results: list[EventDTO] = []
            for ev in cal.events():
                ical = Calendar.from_ical(ev.data)
                for comp in ical.walk("VEVENT"):
                    dto = self._dto_from_ical(comp)
                    if dto is None:
                        continue
                    # Локальная фильтрация по диапазону.
                    # Условие: событие пересекается с [date_from, date_to)
                    if dto.end <= date_from:
                        continue
                    if dto.start >= date_to:
                        continue
                    results.append(dto)
            results.sort(key=lambda x: x.start)
            return results
        return await asyncio.to_thread(_sync)

    async def find_events(
        self,
        matrix_room_id: str,
        room_display_name: str,
        query: str,
        date_from: datetime,
        date_to: datetime,
        creator_user_id: str | None = None,
    ) -> list[EventDTO]:
        """Поиск событий по подстроке (title/description/location)."""
        events = await self.list_events(matrix_room_id, room_display_name, date_from, date_to, creator_user_id=creator_user_id)
        q = query.lower()
        return [
            e for e in events
            if q in (e.title or "").lower()
            or q in (e.description or "").lower()
            or q in (e.location or "").lower()
        ]

    async def delete_event(
        self, matrix_room_id: str, room_display_name: str, uid: str,
        creator_user_id: str | None = None,
    ) -> bool:
        """Удалить событие по uid. True если нашли и удалили."""
        def _sync() -> bool:
            cal = self._get_or_create_collection_sync(matrix_room_id, room_display_name, creator_user_id)
            for ev in cal.events():
                ical = Calendar.from_ical(ev.data)
                for comp in ical.walk("VEVENT"):
                    if str(comp.get("uid")) == uid:
                        ev.delete()
                        return True
            return False
        return await asyncio.to_thread(_sync)


    @staticmethod
    def _dto_from_ical(comp: Any) -> EventDTO | None:
        try:
            uid = str(comp.get("uid"))
            title = str(comp.get("summary", "(без названия)"))
            description = comp.get("description")
            description = str(description) if description else None
            location = comp.get("location")
            location = str(location) if location else None
            # X-LEO-CREATOR — кастомное поле, не входит в стандарт iCal
            # icalendar даёт его через get() как обычное поле
            creator = comp.get("X-LEO-CREATOR")
            creator = str(creator) if creator else None

            dtstart_raw = comp.get("dtstart").dt
            dtend_raw = comp.get("dtend").dt if comp.get("dtend") else None

            # date → datetime (для all-day событий)
            if not isinstance(dtstart_raw, datetime):
                dtstart = datetime.combine(dtstart_raw, datetime.min.time(), ZoneInfo("UTC"))
            else:
                dtstart = dtstart_raw

            if dtend_raw is None:
                dtend = dtstart + timedelta(hours=1)
            elif not isinstance(dtend_raw, datetime):
                dtend = datetime.combine(dtend_raw, datetime.min.time(), ZoneInfo("UTC"))
            else:
                dtend = dtend_raw

            # Гарантируем, что оба datetime aware (с TZ),
            # иначе сравнение с aware-параметрами list_events ломается.
            if dtstart.tzinfo is None:
                dtstart = dtstart.replace(tzinfo=ZoneInfo("UTC"))
            if dtend.tzinfo is None:
                dtend = dtend.replace(tzinfo=ZoneInfo("UTC"))

            tz = str(dtstart.tzinfo)

            return EventDTO(
                uid=uid,
                title=title,
                description=description,
                start=dtstart,
                end=dtend,
                timezone=tz,
                location=location,
                creator_user_id=creator,
            )
        except Exception as e:
            log.error("Failed to parse VEVENT: %s", e)
            return None

