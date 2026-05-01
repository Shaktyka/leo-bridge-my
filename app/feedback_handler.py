# -*- coding: utf-8 -*-
"""
feedback_handler.py — Leo v0.9 feedback panel slash commands.

Slash-команды:
  /feedback <текст>     — оставить комментарий-обратную связь.
                           Если использовано как reply на ответ Leo —
                           привязывается к leo_event_id, иначе general.
  /leo_dashboard [N]    — показать дашборд за последние N дней (default 7).
                           Видно всем (per pilot decision).

Отдельно от kb_handler — feedback это самостоятельная фича.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nio import MatrixRoom

from app.kb_handler import _send_reply  # переиспользуем общий helper

if TYPE_CHECKING:
    from app.bridge import Bridge

log = logging.getLogger(__name__)

# Допустимые имена slash-команд (включая алиасы)
SLASH_PREFIX = ("/feedback", "/leo_dashboard", "/dashboard")


async def try_handle_slash(
    bridge: "Bridge",
    room: MatrixRoom,
    event,
    clean_text: str,
) -> bool:
    """
    Проверяет clean_text на feedback slash-команду и обрабатывает её.
    Возвращает True если команда обработана (тогда bridge не передаёт в Letta).
    """
    text = clean_text.strip()
    if not text.startswith(SLASH_PREFIX):
        return False

    parts = text.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    log.info("[%s] Feedback slash from %s: %s",
             room.room_id[:12], event.sender, cmd)

    if cmd == "/feedback":
        await _cmd_feedback(bridge, room, event, arg)
    elif cmd in ("/leo_dashboard", "/dashboard"):
        await _cmd_dashboard(bridge, room, event, arg)
    else:
        await _send_reply(bridge, room, event,
            f"Неизвестная команда `{cmd}`."
        )
    return True


async def _cmd_feedback(bridge, room, event, arg: str) -> None:
    """
    /feedback <текст> — записать комментарий.
    Если команда — reply на сообщение Leo, привязываем к leo_event_id.
    """
    if not arg:
        await _send_reply(bridge, room, event,
            "Укажи текст комментария: `/feedback <твой комментарий>`\n"
            "Можно также ответить (reply) на конкретное сообщение Leo — "
            "комментарий привяжется к нему."
        )
        return

    if bridge.feedback is None:
        await _send_reply(bridge, room, event,
            "⚠️ Feedback logger не инициализирован."
        )
        return

    # Если это reply на сообщение Leo — берём event_id из m.in_reply_to
    leo_event_id = None
    relates = (event.source.get("content", {}) or {}).get("m.relates_to", {})
    in_reply_to = relates.get("m.in_reply_to") or {}
    target_id = in_reply_to.get("event_id")
    if target_id:
        # Проверим что это реально ответ Leo (есть в feedback.responses)
        try:
            row = await bridge.feedback.pg.fetchval(
                "SELECT 1 FROM feedback.responses WHERE leo_event_id = $1",
                target_id,
            )
            if row:
                leo_event_id = target_id
        except Exception as e:
            log.warning("feedback reply-target lookup failed: %s", e)

    await bridge.feedback.log_comment(
        matrix_user_id=event.sender,
        room_id=room.room_id,
        comment_text=arg,
        leo_event_id=leo_event_id,
    )

    if leo_event_id:
        msg = "📝 Комментарий привязан к ответу Leo. Спасибо!"
    else:
        msg = "📝 Комментарий записан (без привязки к конкретному ответу). Спасибо!"
    await _send_reply(bridge, room, event, msg)


async def _cmd_dashboard(bridge, room, event, arg: str) -> None:
    """
    /leo_dashboard [N]  — за последние N дней (default 7).
    Видно всем пользователям.
    """
    if bridge.feedback is None:
        await _send_reply(bridge, room, event,
            "⚠️ Feedback logger не инициализирован."
        )
        return

    period_days = 7
    if arg:
        try:
            n = int(arg.split()[0])
            if 1 <= n <= 365:
                period_days = n
            else:
                await _send_reply(bridge, room, event,
                    "Период должен быть от 1 до 365 дней."
                )
                return
        except ValueError:
            await _send_reply(bridge, room, event,
                f"Не понял аргумент `{arg}`. Используй: `/leo_dashboard 30`"
            )
            return

    try:
        report = await bridge.feedback.dashboard(period_days=period_days)
    except Exception as e:
        log.exception("dashboard failed: %s", e)
        await _send_reply(bridge, room, event,
            f"⚠️ Не удалось построить дашборд: {e}"
        )
        return

    await _send_reply(bridge, room, event, report)
