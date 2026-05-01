# -*- coding: utf-8 -*-
"""
leo_handler.py — Leo v1.2.0 — системные slash-команды.
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING
from nio import MatrixRoom
from app.kb_handler import _send_reply
if TYPE_CHECKING:
    from app.bridge import Bridge
log = logging.getLogger(__name__)
SLASH_PREFIX = ("/leo_help", "/leo_reset")

async def try_handle_slash(bridge, room, event, clean_text):
    text = clean_text.strip()
    if not text.startswith(SLASH_PREFIX):
        return False
    parts = text.split(maxsplit=1)
    cmd = parts[0]
    log.info("[%s] Leo slash from %s: %s", room.room_id[:12], event.sender, cmd)
    if cmd == "/leo_help":
        await _cmd_help(bridge, room, event)
    elif cmd == "/leo_reset":
        await _cmd_reset(bridge, room, event)
    else:
        await _send_reply(bridge, room, event, f"Неизвестная команда `{cmd}`.")
    return True

HELP_TEXT = """# 🦉 Leo — Корпоративный AI-ассистент

Чтобы обратиться в групповой комнате — упомяни **@leo**. В личной пиши напрямую.

---

## 📅 Календарь

**Создать событие:**
- `создай встречу "Демо" завтра в 14:00 на час`
- `запланируй созвон в пятницу 15:00`

**Напоминания 🆕 (v1.1.0):**
- `встреча завтра в 10:00, напомни за час`
- `созвон в 15:00, напомни за 30 минут`
- `встреча в понедельник, напомни за день`
- `встреча в пятницу без напоминания`
- без указания — напомнит за **15 минут** по умолчанию

**Просмотр и поиск:**
- `что у меня на сегодня?`
- `покажи календарь на эту неделю`
- `найди встречу с Петровым`

**Отмена:**
- `отмени созвон с командой`

---

## 🔍 База знаний

**Личная KB:**
- `/add_to_kb` (reply на файл) — добавить документ
- `что я знаю про проект X?` — поиск в личной базе

**Корпоративная KB:**
- `найди в корпоративной базе регламент командировок`

---

## 🌐 Веб-поиск

- `найди последние новости про Python 3.13`
- `что такое RFC 5545?`
- `какой курс доллара сегодня?`

---

## 🛠 Системные команды

- `/leo_help` — эта справка
- `/leo_reset` — очистить память Leo в этой комнате
- `/feedback <текст>` — оставить отзыв о работе Leo
- `/leo_dashboard` — статистика и метрики
- `/kb_help` — справка по базе знаний
- `/kb_list` — список документов в личной KB

---

## 💡 Советы

- Leo помнит контекст разговора **в каждой комнате отдельно**
- Если Leo стал отвечать медленно — напиши `/leo_reset`
- Напоминания приходят в эту же комнату — E2EE зашифрованными

Версия: **v1.1.0** · Модель: Claude Sonnet 4.6 · Respect.Chat
"""

async def _cmd_help(bridge, room, event):
    await _send_reply(bridge, room, event, HELP_TEXT)

async def _cmd_reset(bridge, room, event):
    """v1.4.0b: переиспользует bridge._compact_agent (общий код с auto-compact)."""
    await _send_reply(bridge, room, event,
        "🔄 Начинаю сброс памяти... Займёт несколько секунд."
    )
    room_id = room.room_id
    try:
        new_agent_id = await bridge._compact_agent(
            room_id=room_id,
            room_name=room.display_name or room_id,
            matrix_user_id=event.sender,
            reason="manual",
        )
        if not new_agent_id:
            await _send_reply(bridge, room, event,
                "⚠️ Агент для этой комнаты не найден или сброс не удался. "
                "Напиши что-нибудь — Leo создаст агента при следующем сообщении."
            )
            return

        await _send_reply(bridge, room, event,
            "✅ **Память очищена!**\n\n"
            "Я готов к работе с чистого листа. "
            "Контекст прошлых разговоров сброшен, "
            "но календарь и база знаний сохранены. Чем могу помочь? 🦉"
        )
    except Exception as e:
        log.exception("[%s] /leo_reset failed: %s", room_id[:12], e)
        await _send_reply(bridge, room, event,
            f"⚠️ Не удалось сбросить память: `{type(e).__name__}: {e}`"
        )
