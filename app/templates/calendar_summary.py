"""
v1.8.1 шаблон #5: calendar_summary с улучшенным пояснением периода.

Изменения относительно v1.8.0:
- Период: прошлая завершённая неделя (Mon 00:00 — Sun 23:59 UTC).
  Текущая неделя НЕ включается. Это семантика "прошлая завершённая неделя".
- _empty_doc явно объясняет какой период взят и как расширить (weeks_back=2).
- Даты в выводе локализованы на русский ("20 апреля — 26 апреля").
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from app.templates.base import (
    fmt_date,
    gpt_summarize,
)


SYSTEM_PROMPT = (
    "Ты ассистент по продуктивности. На основе списка встреч сотрудника "
    "за прошедшую неделю составь краткую ретроспективу:\n\n"
    "1. **Главные темы недели** — выдели 2-4 основные направления по названиям встреч\n"
    "2. **Загрузка** — оцени общую загрузку (нормальная / высокая / критичная) "
    "с цифрами часов\n"
    "3. **Регулярные встречи** — отметь повторяющиеся (напр. ежедневные стендапы)\n"
    "4. **Наблюдения** — что-то заметное: длинные встречи, плотные дни, паузы\n\n"
    "Пиши кратко (не больше 250 слов). Используй Markdown с заголовками ##. "
    "Только то что есть в данных. Не выдумывай. Если данных мало — пиши лаконично."
)


_MONTHS_RU_GENITIVE = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _fmt_date_ru(dt: datetime) -> str:
    """2026-04-20 → '20 апреля 2026'. Год опускается если совпадает с текущим."""
    today = datetime.now(timezone.utc)
    if dt.year == today.year:
        return f"{dt.day} {_MONTHS_RU_GENITIVE[dt.month]}"
    return f"{dt.day} {_MONTHS_RU_GENITIVE[dt.month]} {dt.year}"


def _format_event_line(ev_dict: dict, user_tz: str = "UTC") -> str:
    """Одна строка события для вывода в Markdown."""
    start = ev_dict.get("start")
    end = ev_dict.get("end")
    title = (ev_dict.get("title") or "—").strip()
    location = (ev_dict.get("location") or "").strip()

    if isinstance(start, str):
        try:
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except Exception:
            start = None
    if isinstance(end, str):
        try:
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except Exception:
            end = None

    time_str = "—"
    duration_str = ""
    if start and end:
        time_str = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        dur_min = int((end - start).total_seconds() / 60)
        if dur_min >= 60:
            h = dur_min // 60
            m = dur_min % 60
            duration_str = f" ({h}ч{m:02d}м)" if m else f" ({h}ч)"
        else:
            duration_str = f" ({dur_min}м)"

    line = f"- **{time_str}**{duration_str} {title}"
    if location:
        line += f"  _@ {location}_"
    return line


def _stats_from_events(events: list[dict]) -> dict:
    if not events:
        return {
            "count": 0, "total_minutes": 0, "longest_minutes": 0,
            "by_day_count": {}, "repeating": [],
        }

    total_min = 0
    longest_min = 0
    by_day_count: dict[str, int] = defaultdict(int)
    titles_count: Counter = Counter()

    for ev in events:
        start = ev.get("start")
        end = ev.get("end")
        if isinstance(start, str):
            try:
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except Exception:
                continue
        if isinstance(end, str):
            try:
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except Exception:
                continue
        if not start or not end:
            continue

        dur_min = int((end - start).total_seconds() / 60)
        total_min += max(dur_min, 0)
        if dur_min > longest_min:
            longest_min = dur_min

        day_key = start.strftime("%Y-%m-%d")
        by_day_count[day_key] += 1

        title_norm = (ev.get("title") or "").strip().lower()
        if title_norm:
            titles_count[title_norm] += 1

    repeating = [
        (title, cnt) for title, cnt in titles_count.most_common()
        if cnt >= 3
    ]

    return {
        "count": len(events),
        "total_minutes": total_min,
        "longest_minutes": longest_min,
        "by_day_count": dict(by_day_count),
        "repeating": repeating,
    }


def _ascii_chart(by_day: dict[str, int], width: int = 30) -> str:
    if not by_day:
        return ""
    max_v = max(by_day.values())
    if max_v == 0:
        return ""
    sorted_days = sorted(by_day.keys())
    title = "Встречи по дням"
    lines = ["```", title, "─" * len(title)]
    for day in sorted_days:
        v = by_day[day]
        bar_len = int(round(v / max_v * width))
        bar = "█" * max(bar_len, 1 if v > 0 else 0)
        lines.append(f"{day}  {bar} {v}")
    lines.append("```")
    return "\n".join(lines)


def _calculate_period(weeks_back: int) -> tuple[datetime, datetime]:
    """Прошлая(ие) завершённая(ые) календарная(ые) неделя(и) Mon-Sun.

    weeks_back=1 → последняя завершённая Mon-Sun
    weeks_back=2 → последние 2 завершённые недели (14 дней)

    Возвращает (period_start, period_end). period_end ИСКЛЮЧИТЕЛЬНО
    (= понедельник 00:00 текущей недели).
    """
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    # weekday: 0=Mon, 6=Sun
    this_monday = today_start - timedelta(days=today_start.weekday())
    period_end = this_monday  # exclusive — Mon 00:00 текущей недели
    period_start = this_monday - timedelta(weeks=weeks_back)
    return period_start, period_end


async def render(
    *,
    params: dict[str, Any],
    leo_pool: asyncpg.Pool,
    matrix_room_id: str,
    matrix_user_id: str,
    cal_client: Any = None,
) -> dict[str, Any]:
    weeks_back = int(params.get("weeks_back", 1))
    weeks_back = max(1, min(8, weeks_back))

    if cal_client is None:
        return _empty_doc(
            "Обзор календаря",
            "Календарный клиент недоступен на сервере.",
            weeks_back=weeks_back,
        )

    period_start, period_end = _calculate_period(weeks_back)
    # Для отображения юзеру: последний день периода = period_end - 1 day (включительно Sun)
    period_end_inclusive = period_end - timedelta(days=1)

    period_str = f"{_fmt_date_ru(period_start)} — {_fmt_date_ru(period_end_inclusive)}"

    try:
        events_dto = await cal_client.list_events(
            matrix_room_id=matrix_room_id,
            room_display_name="calendar_summary",
            date_from=period_start,
            date_to=period_end,
            creator_user_id=matrix_user_id,
        )
    except Exception as e:
        return _empty_doc(
            f"Обзор календаря за {weeks_back} нед.",
            f"Ошибка чтения календаря: {e}",
            weeks_back=weeks_back,
            period_str=period_str,
        )

    events = [e.to_dict() for e in events_dto] if events_dto else []

    if not events:
        # Улучшенное пояснение для пустого результата
        explanation = (
            f"За период **{period_str}** (прошлая завершённая "
            f"{'неделя' if weeks_back == 1 else f'{weeks_back} нед.'}) "
            f"в вашем календаре встреч не зафиксировано.\n\n"
            f"### Почему период такой?\n\n"
            f"Шаблон показывает **завершённые** недели Mon-Sun. "
            f"Текущая неделя (которая ещё идёт) не включается — это сделано "
            f"для ретроспективного анализа.\n\n"
            f"### Что попробовать?\n\n"
            f"- **Посмотреть события включая текущую неделю** — попроси "
            f"вместо этого `calendar_list_events` за нужный период\n"
            f"- **Расширить период** — повтори с параметром "
            f"`weeks_back=2` (или больше) чтобы захватить шире.\n"
            f"- **Свежие данные за «прошлую неделю» в широком смысле** — "
            f"уточни какие именно даты тебя интересуют."
        )
        return _empty_doc(
            f"Обзор календаря — {period_str}",
            explanation,
            weeks_back=weeks_back,
            period_str=period_str,
        )

    stats = _stats_from_events(events)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        start = ev.get("start")
        if isinstance(start, str):
            try:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except Exception:
                continue
        elif isinstance(start, datetime):
            start_dt = start
        else:
            continue
        day_key = start_dt.strftime("%Y-%m-%d")
        by_day[day_key].append(ev)

    # LLM summary
    events_for_llm: list[str] = []
    for ev in events:
        start = ev.get("start", "")
        end = ev.get("end", "")
        title = ev.get("title", "")
        events_for_llm.append(f"{start} — {end} :: {title}")

    user_prompt = (
        f"Период: {period_str}\n"
        f"Всего встреч: {stats['count']}\n"
        f"Суммарно во встречах: {stats['total_minutes'] // 60}ч"
        f"{stats['total_minutes'] % 60:02d}м\n"
        f"\n"
        f"События:\n" + "\n".join(events_for_llm)
    )

    summary_md = await gpt_summarize(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=800,
    )

    # Документ
    lines: list[str] = []
    lines.append(f"# Обзор календаря — {period_str}")
    lines.append("")
    lines.append(
        f"*Период: {fmt_date(period_start)} — {fmt_date(period_end_inclusive)} "
        f"(прошлая завершённая "
        f"{'неделя' if weeks_back == 1 else f'{weeks_back} нед.'}).*"
    )
    lines.append("")
    lines.append(f"**Всего встреч:** {stats['count']}  ")
    total_h = stats["total_minutes"] // 60
    total_m = stats["total_minutes"] % 60
    lines.append(f"**Суммарно во встречах:** {total_h}ч {total_m}м  ")
    if stats["longest_minutes"]:
        lh = stats["longest_minutes"] // 60
        lm = stats["longest_minutes"] % 60
        if lh:
            lines.append(f"**Самая длинная встреча:** {lh}ч {lm}м")
        else:
            lines.append(f"**Самая длинная встреча:** {lm}м")
    lines.append("")

    chart = _ascii_chart(stats["by_day_count"])
    if chart:
        lines.append(chart)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Ретроспектива (LLM)")
    lines.append("")
    lines.append(
        "*Краткая сводка сгенерирована GPT-4o-mini на основе ваших встреч за период. "
        "Это инструмент для саморефлексии — проверяйте перед использованием.*"
    )
    lines.append("")
    lines.append(summary_md)
    lines.append("")

    if stats["repeating"]:
        lines.append("---")
        lines.append("")
        lines.append("## Регулярные встречи")
        lines.append("")
        for title, cnt in stats["repeating"]:
            lines.append(f"- **{title}** — {cnt} раз")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Все встречи по дням")
    lines.append("")

    for day in sorted(by_day.keys()):
        items = by_day[day]
        weekday_ru = _weekday_ru(day)
        lines.append(f"### {day} ({weekday_ru}) — {len(items)} встр.")
        lines.append("")
        for ev in items:
            lines.append(_format_event_line(ev))
            desc = (ev.get("description") or "").strip()
            if desc and len(desc) <= 200:
                lines.append(f"  > {desc}")
        lines.append("")

    content_md = "\n".join(lines)
    today = datetime.now().strftime("%Y%m%d")
    return {
        "filename": f"calendar_summary_{today}_{weeks_back}w",
        "format": "docx",
        "title": f"Обзор календаря {period_str}",
        "content_md": content_md,
        "cache_hit": False,
    }


_WEEKDAYS_RU = {
    0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
    4: "пятница", 5: "суббота", 6: "воскресенье",
}


def _weekday_ru(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return _WEEKDAYS_RU[d.weekday()]
    except Exception:
        return ""


def _empty_doc(
    title: str,
    message: str,
    *,
    weeks_back: int = 1,
    period_str: str = "",
) -> dict[str, Any]:
    """Пустой/информационный документ. v1.8.1: подробное пояснение периода."""
    md = f"# {title}\n\n{message}\n"
    return {
        "filename": title.lower().replace(" ", "_").replace("—", "_")[:40] + "_empty",
        "format": "md",
        "title": title,
        "content_md": md,
        "cache_hit": False,
    }
