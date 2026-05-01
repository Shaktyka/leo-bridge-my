# -*- coding: utf-8 -*-
"""
feedback_logger.py — Leo v0.9 feedback panel.

Centralized logger for:
- Each Leo response (auto-logged after successful room_send)
- Matrix m.reaction events on Leo replies
- Free-form /feedback commands
- /leo_dashboard report generation (Markdown)

Schema: feedback.* in ai_assistant database (see 001_feedback_schema.sql).

Design principles:
- Never blocks the main flow: all DB calls wrapped in try/except, log+swallow on error
- Truncates long content to 5000 chars for storage
- Uses connection pool injected from bridge (no own pool)
- Idempotent reaction insert via ON CONFLICT DO NOTHING
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# Maximum length of stored user_message and leo_reply (chars).
# Beyond this we truncate. 5000 chars covers ~99% of realistic conversations.
MAX_TEXT_LEN = 5000

# Reaction emojis used in dashboard categorization
POSITIVE_REACTIONS = {"👍", "❤️", "🎉", "🔥", "💯"}
NEGATIVE_REACTIONS = {"👎", "❌", "💩"}
PARTIAL_REACTIONS = {"🤔", "😐"}


@dataclass
class ResponseRecord:
    """Container passed from bridge to log_response()."""
    matrix_user_id: str
    room_id: str
    user_event_id: str
    leo_event_id: str
    user_message: str
    leo_reply: str
    agent_id: Optional[str] = None
    tools_called: list[str] = field(default_factory=list)
    steps_count: Optional[int] = None
    context_tokens: Optional[int] = None
    response_time_ms: Optional[int] = None
    postprocess_hit: bool = False
    is_slash_command: bool = False


class FeedbackLogger:
    """
    Sit alongside bridge, log everything into feedback.* schema.

    Usage:
        flogger = FeedbackLogger(pg_pool)
        await flogger.log_response(record)
        await flogger.log_reaction(...)
        await flogger.log_comment(...)
        markdown_report = await flogger.dashboard(period_days=7)
    """

    def __init__(self, pg_pool: asyncpg.Pool):
        self.pg = pg_pool

    # =================================================================
    # Recording
    # =================================================================

    async def log_response(self, rec: ResponseRecord) -> None:
        """Log every Leo response. Called after successful room_send."""
        user_msg = (rec.user_message or "")[:MAX_TEXT_LEN]
        leo_reply = (rec.leo_reply or "")[:MAX_TEXT_LEN]
        try:
            await self.pg.execute(
                """
                INSERT INTO feedback.responses (
                    matrix_user_id, room_id, user_event_id, leo_event_id,
                    agent_id, user_message, leo_reply, tools_called,
                    steps_count, context_tokens, response_time_ms,
                    postprocess_hit, is_slash_command
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (leo_event_id) DO NOTHING
                """,
                rec.matrix_user_id, rec.room_id, rec.user_event_id,
                rec.leo_event_id, rec.agent_id, user_msg, leo_reply,
                rec.tools_called or [], rec.steps_count, rec.context_tokens,
                rec.response_time_ms, rec.postprocess_hit, rec.is_slash_command,
            )
        except Exception as e:
            # Never block main flow — just log and continue
            log.warning("feedback.log_response failed for leo_event_id=%s: %s",
                        rec.leo_event_id, e)

    async def log_reaction(
        self,
        leo_event_id: str,
        matrix_user_id: str,
        room_id: str,
        reaction: str,
        reaction_event_id: str,
    ) -> bool:
        """
        Log a Matrix m.reaction event on a Leo reply.
        Returns True if the reaction was new (inserted), False if duplicate
        or target is not a Leo response.
        """
        try:
            # Verify that leo_event_id corresponds to a logged Leo response
            target = await self.pg.fetchval(
                "SELECT id FROM feedback.responses WHERE leo_event_id = $1",
                leo_event_id,
            )
            if target is None:
                # Reaction on something that is NOT a Leo reply — ignore
                return False

            result = await self.pg.execute(
                """
                INSERT INTO feedback.reactions (
                    leo_event_id, matrix_user_id, room_id, reaction,
                    reaction_event_id
                ) VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (leo_event_id, matrix_user_id, reaction) DO NOTHING
                """,
                leo_event_id, matrix_user_id, room_id, reaction,
                reaction_event_id,
            )
            # asyncpg returns "INSERT 0 1" if inserted, "INSERT 0 0" if conflict
            return result.endswith(" 1")
        except Exception as e:
            log.warning("feedback.log_reaction failed: %s", e)
            return False

    async def remove_reaction(self, reaction_event_id: str) -> bool:
        """
        Remove a reaction by its m.reaction event_id.
        Used when user redacts the reaction in Element.
        """
        try:
            result = await self.pg.execute(
                "DELETE FROM feedback.reactions WHERE reaction_event_id = $1",
                reaction_event_id,
            )
            return result.endswith(" 1")
        except Exception as e:
            log.warning("feedback.remove_reaction failed: %s", e)
            return False

    async def log_comment(
        self,
        matrix_user_id: str,
        room_id: str,
        comment_text: str,
        leo_event_id: Optional[str] = None,
    ) -> None:
        """Log a free-form /feedback comment."""
        try:
            await self.pg.execute(
                """
                INSERT INTO feedback.comments (
                    leo_event_id, matrix_user_id, room_id, comment_text
                ) VALUES ($1, $2, $3, $4)
                """,
                leo_event_id, matrix_user_id, room_id, comment_text[:MAX_TEXT_LEN],
            )
        except Exception as e:
            log.warning("feedback.log_comment failed: %s", e)

    # =================================================================
    # Dashboard
    # =================================================================

    async def dashboard(
        self,
        period_days: int = 7,
        room_id: Optional[str] = None,
    ) -> str:
        """
        Build a Markdown report for the last `period_days` days.
        If `room_id` is given, scope is that room only; otherwise global.
        Visible to all users (per pilot decision).
        Returns Markdown string ready for Element.
        """
        try:
            since = datetime.now(timezone.utc) - timedelta(days=period_days)
            scope_label = f"комнате {room_id}" if room_id else "всех комнатах"
            room_filter = "AND r.room_id = $2" if room_id else ""
            params: list = [since]
            if room_id:
                params.append(room_id)

            # Totals
            totals = await self.pg.fetchrow(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE is_slash_command) AS slash,
                    COUNT(*) FILTER (WHERE NOT is_slash_command) AS llm,
                    COUNT(*) FILTER (WHERE postprocess_hit) AS pp_hits,
                    COUNT(DISTINCT matrix_user_id) AS users,
                    AVG(response_time_ms) FILTER (WHERE NOT is_slash_command)::INT
                        AS avg_llm_ms,
                    AVG(context_tokens) FILTER (WHERE NOT is_slash_command)::INT
                        AS avg_ctx
                FROM feedback.responses r
                WHERE r.created_at >= $1 {room_filter}
                """,
                *params,
            )

            # Reaction breakdown
            reactions = await self.pg.fetch(
                f"""
                SELECT rx.reaction, COUNT(*) AS cnt
                FROM feedback.reactions rx
                JOIN feedback.responses r ON r.leo_event_id = rx.leo_event_id
                WHERE rx.reacted_at >= $1 {room_filter}
                GROUP BY rx.reaction
                ORDER BY cnt DESC
                """,
                *params,
            )

            # Top users by negative reactions (only when not scoped to a room)
            top_negatives = []
            if not room_id:
                top_negatives = await self.pg.fetch(
                    """
                    SELECT r.matrix_user_id,
                           COUNT(*) FILTER (WHERE rx.reaction = ANY($2::text[])) AS neg
                    FROM feedback.responses r
                    LEFT JOIN feedback.reactions rx ON rx.leo_event_id = r.leo_event_id
                    WHERE r.created_at >= $1
                    GROUP BY r.matrix_user_id
                    HAVING COUNT(*) FILTER (WHERE rx.reaction = ANY($2::text[])) > 0
                    ORDER BY neg DESC
                    LIMIT 5
                    """,
                    since, list(NEGATIVE_REACTIONS),
                )

            # Tool usage
            tools = await self.pg.fetch(
                f"""
                SELECT unnest(tools_called) AS tool, COUNT(*) AS cnt
                FROM feedback.responses r
                WHERE r.created_at >= $1 {room_filter}
                  AND array_length(tools_called, 1) > 0
                GROUP BY tool
                ORDER BY cnt DESC
                LIMIT 10
                """,
                *params,
            )

            # Recent comments (last 5)
            recent_comments = await self.pg.fetch(
                f"""
                SELECT c.created_at, c.matrix_user_id, c.comment_text
                FROM feedback.comments c
                WHERE c.created_at >= $1
                  {("AND c.room_id = $2" if room_id else "")}
                ORDER BY c.created_at DESC
                LIMIT 5
                """,
                *params,
            )

            # v1.0.3: Алерт на затравленные агенты — agents requiring attention
            alert_agents = await self.pg.fetch(
                f"""
                SELECT
                    r.agent_id,
                    COUNT(*) AS responses_count,
                    AVG(r.context_tokens)::INT AS avg_tokens,
                    (AVG(r.response_time_ms) / 1000)::NUMERIC(10,1) AS avg_sec,
                    COUNT(*) FILTER (WHERE r.postprocess_hit) AS pp_hits,
                    array_agg(DISTINCT r.matrix_user_id) AS users
                FROM feedback.responses r
                WHERE r.created_at >= $1
                  AND NOT r.is_slash_command
                  AND r.agent_id IS NOT NULL
                  {room_filter}
                GROUP BY r.agent_id
                HAVING (AVG(r.context_tokens) > 30000)
                    OR (AVG(r.response_time_ms) > 30000)
                    OR (COUNT(*) FILTER (WHERE r.postprocess_hit) >= 5)
                ORDER BY avg_tokens DESC NULLS LAST
                LIMIT 10
                """,
                *params,
            )

            # v1.2.3: Тренд по дням (последние period_days дней)
            daily = await self.pg.fetch(
                f"""
                SELECT
                    DATE(r.created_at AT TIME ZONE 'Europe/Moscow') AS day,
                    COUNT(*) FILTER (WHERE NOT r.is_slash_command) AS llm_cnt
                FROM feedback.responses r
                WHERE r.created_at >= $1 {room_filter}
                GROUP BY day
                ORDER BY day
                """,
                *params,
            )

            # v0.9.4: Build Markdown with proper bullet lists
            lines = [
                f"📊 **Leo Dashboard — {period_days} дн.** ({scope_label})",
                "",
            ]

            # v1.2.3: Тренд-чарт по дням
            if daily:
                max_cnt = max((r["llm_cnt"] or 0) for r in daily) or 1
                BAR_MAX = 12
                lines.append("**📈 Активность по дням:**")
                lines.append("```")
                for row in daily:
                    day_str = row["day"].strftime("%d.%m")
                    cnt = row["llm_cnt"] or 0
                    bar_len = round(cnt / max_cnt * BAR_MAX)
                    bar = "█" * bar_len
                    lines.append(f"{day_str} {bar:<{BAR_MAX}} {cnt}")
                lines.append("```")
                lines.append("")
            total = totals["total"] or 0
            if total == 0:
                lines.append("_Нет данных за указанный период._")
                return "\n".join(lines)

            # v1.0.3: ⚠️ Внимание — проблемные агенты (если есть)
            if alert_agents:
                lines.append("**⚠️ Внимание (агенты требуют проверки):**")
                lines.append("")
                for a in alert_agents:
                    aid_short = (a["agent_id"] or "?")[:16] + "…"
                    users_list = a["users"] or []
                    user_label = users_list[0] if len(users_list) == 1 else f"{len(users_list)} users"
                    issues = []
                    if a["avg_tokens"] and a["avg_tokens"] > 30000:
                        issues.append(f"контекст **{a['avg_tokens']} токенов**")
                    if a["avg_sec"] and float(a["avg_sec"]) > 30:
                        issues.append(f"время **{a['avg_sec']} сек**")
                    if a["pp_hits"] and a["pp_hits"] >= 5:
                        issues.append(f"postprocess **{a['pp_hits']}×**")
                    lines.append(
                        f"- `{aid_short}` ({user_label}): "
                        + ", ".join(issues)
                        + f"  · {a['responses_count']} ответ(ов)"
                    )
                lines.append("")

            # Сводка
            lines.append("**📈 Активность:**")
            lines.append("")
            lines.append(f"- Всего ответов: **{total}**")
            lines.append(f"- LLM-ответов: {totals['llm']}")
            lines.append(f"- Slash-команд: {totals['slash']}")
            lines.append(f"- Уникальных пользователей: {totals['users']}")
            lines.append("")

            if totals["llm"]:
                lines.append("**⏱ Производительность:**")
                lines.append("")
                lines.append(
                    f"- Среднее время LLM-ответа: **{(totals['avg_llm_ms'] or 0) / 1000:.1f} сек**"
                )
                lines.append(
                    f"- Средний контекст: **{totals['avg_ctx'] or 0} токенов**"
                )
                lines.append("")

            if totals["pp_hits"]:
                pct = totals["pp_hits"] * 100 / max(totals["llm"], 1)
                lines.append("**🛡 Защита от галлюцинаций:**")
                lines.append("")
                lines.append(
                    f"- Postprocess сработал: **{totals['pp_hits']} раз** "
                    f"({pct:.1f}% от LLM-ответов)"
                )
                lines.append("")

            # Реакции
            if reactions:
                lines.append("**Реакции пользователей:**")
                lines.append("")
                for row in reactions:
                    lines.append(f"- {row['reaction']} — **{row['cnt']}**")
                lines.append("")
            else:
                lines.append("_Реакций пока не было._")
                lines.append("")

            # Топ negatives
            if top_negatives:
                lines.append("**Топ пользователей по 👎/❌:**")
                lines.append("")
                for i, row in enumerate(top_negatives, 1):
                    lines.append(f"{i}. {row['matrix_user_id']} — **{row['neg']}**")
                lines.append("")

            # Tools
            if tools:
                lines.append("**Топ tools:**")
                lines.append("")
                for row in tools:
                    lines.append(f"- `{row['tool']}` — **{row['cnt']}**")
                lines.append("")

            # Recent comments
            if recent_comments:
                lines.append("**Последние комментарии (/feedback):**")
                lines.append("")
                for c in recent_comments:
                    when = c["created_at"].strftime("%d.%m %H:%M")
                    txt = c["comment_text"][:120]
                    if len(c["comment_text"]) > 120:
                        txt += "…"
                    lines.append(f"- _{when}_ **{c['matrix_user_id']}:** {txt}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            log.exception("feedback.dashboard failed: %s", e)
            return f"❌ Не удалось построить дашборд: {e}"
