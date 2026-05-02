#!/usr/bin/env python3
"""
v1.7.0 patcher: добавляет в /opt/ai/bridge/scripts/register_tools.py
два новых tool'а:
- leo_list_templates() — список доступных шаблонов
- leo_render_template(name, ...) — рендерит шаблон и отправляет в чат

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/register_tools.py")
BACKUP = Path("/opt/ai/bridge/scripts/register_tools.py.bak-pre-v170")


MARKER_BEFORE_REGISTRATION = """# ============================================================
# Регистрация в Letta
# ============================================================"""

NEW_FUNCTIONS = '''
def leo_list_templates() -> str:
    """
    Получить список доступных шаблонов отчётов в корпоративной KB Респект.Чата.

    Шаблоны — это типовые отчёты которые Leo может сгенерировать как docx-файл
    на основе данных KB. Каждый шаблон имеет имя, описание и набор параметров.

    Returns:
        Текстовый список шаблонов с описанием параметров.

    Когда использовать:
        - Пользователь спрашивает "какие отчёты ты умеешь делать", "что есть из готовых"
        - Тебе самому нужно вспомнить точное имя шаблона перед leo_render_template
        - Пользователь не уточнил какой именно отчёт хочет — покажи варианты
    """
    import os
    import httpx

    try:
        r = httpx.post(
            "http://127.0.0.1:8284/templates/list",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json={},
            timeout=10,
        )
    except Exception as e:
        return f"Ошибка соединения: {e}"

    if r.status_code >= 400:
        return f"Ошибка API: {r.status_code} {r.text[:200]}"

    data = r.json()
    tmpls = data.get("templates", [])
    if not tmpls:
        return "Шаблоны пока не настроены."

    lines = [f"Доступно шаблонов отчётов: {len(tmpls)}", ""]
    for t in tmpls:
        lines.append(f"### {t['name']}")
        lines.append(t.get("description", ""))
        params = t.get("params", {})
        if params:
            lines.append("Параметры:")
            for pname, pinfo in params.items():
                ptype = pinfo.get("type", "?")
                pdefault = pinfo.get("default")
                pdesc = pinfo.get("description", "")
                default_str = f" (по умолчанию: {pdefault})" if pdefault is not None else ""
                lines.append(f"  - {pname}: {ptype}{default_str} — {pdesc}")
        lines.append("")
    return "\\n".join(lines)


def leo_render_template(
    name: str,
    matrix_room_id: str,
    matrix_user_id: str,
    weeks_back: int = 0,
    days_back: int = 0,
    competitor: str = "",
    topic: str = "",
    limit: int = 0,
) -> str:
    """
    Сгенерировать готовый отчёт по шаблону на основе корпоративной KB Респект.Чата
    и отправить в Matrix-чат как docx-файл.

    Args:
        name: имя шаблона. Поддерживаемые:
              - weekly_infopovody (параметр: weeks_back)
              - kb_changes_digest (параметр: days_back)
              - competitor_summary (параметры: competitor, limit)
              - topic_compendium (параметры: topic, limit)
              Получить актуальный список: leo_list_templates()
        matrix_room_id: ID Matrix-комнаты, начинается с "!". Возьми из контекста.
        matrix_user_id: MXID пользователя из [from=...] для ACL-проверки.
        weeks_back: для weekly_infopovody — за сколько недель (1-12, по умолчанию 1).
        days_back: для kb_changes_digest — за сколько дней (1-90, по умолчанию 7).
        competitor: для competitor_summary — название (Гарант, Актион, ...).
        topic: для topic_compendium — тема поиска ("командировки", "НДФЛ", ...).
        limit: для competitor_summary и topic_compendium — кол-во материалов
               (1-50, по умолчанию 15 для competitor, 20 для compendium).

    Returns:
        Подтверждение с именем сгенерированного файла или описание ошибки.

    Когда использовать:
        - Пользователь просит "сделай еженедельный обзор инфоповодов" → weekly_infopovody
        - "Что у нас обновилось в KB за последнюю неделю" → kb_changes_digest
        - "Сделай сводку по Гаранту" / "что есть про КонсультантПлюс" → competitor_summary
        - "Подбери всё про командировки" / "сделай подборку по НДФЛ" → topic_compendium

    Когда НЕ использовать:
        - Простой поисковый запрос — используй respect_kb_search вместо этого
        - Если шаблон не подходит под запрос пользователя — лучше respect_kb_search
        - Если пользователь хочет сам читать ответ в чате (не файлом) — respect_kb_search
    """
    import os
    import httpx

    if not name:
        return "Не указано имя шаблона. Получи список: leo_list_templates()."
    if not matrix_room_id:
        return "Не указан matrix_room_id."
    if not matrix_user_id:
        return "Не указан matrix_user_id (нужен для проверки прав доступа к KB)."

    # Собираем params: кладём только те что не равны дефолтам ("0", "")
    params: dict = {}
    if weeks_back > 0:
        params["weeks_back"] = weeks_back
    if days_back > 0:
        params["days_back"] = days_back
    if competitor:
        params["competitor"] = competitor
    if topic:
        params["topic"] = topic
    if limit > 0:
        params["limit"] = limit

    body = {
        "name": name,
        "params": params,
        "matrix_room_id": matrix_room_id,
        "matrix_user_id": matrix_user_id,
    }

    try:
        r = httpx.post(
            "http://127.0.0.1:8284/templates/render",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json=body,
            timeout=180,  # competitor_summary с LLM может занять до минуты
        )
    except Exception as e:
        return f"Ошибка соединения: {e}"

    if r.status_code == 400:
        return f"Ошибка параметров: {r.text[:300]}"
    if r.status_code == 503:
        return "Сервис не готов — попробуй ещё раз через минуту."
    if r.status_code >= 400:
        return f"Ошибка генерации шаблона: HTTP {r.status_code}: {r.text[:300]}"

    data = r.json()
    fn = data.get("filename", "report")
    fmt = data.get("format", "docx").upper()
    size_kb = (data.get("size_bytes") or 0) / 1024
    title = data.get("title") or fn
    return (
        f"Готово. «{title}» — файл {fn} ({size_kb:.1f} KB, {fmt}) "
        f"отправлен в чат."
    )


'''

# Маркер для добавления в TOOLS
MARKER_TOOLS_LAST_LINE = "    respect_kb_search,\n]"
NEW_TOOLS_LIST = "    respect_kb_search,\n    leo_list_templates,\n    leo_render_template,\n]"


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "def leo_render_template(" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if MARKER_BEFORE_REGISTRATION not in text:
        print("ERROR: registration-section marker not found", file=sys.stderr)
        return 3
    if MARKER_TOOLS_LAST_LINE not in text:
        print(
            "ERROR: TOOLS-list marker not found "
            "(ожидался ', respect_kb_search,\\n]' — был ли применён v160?)",
            file=sys.stderr,
        )
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(
        MARKER_BEFORE_REGISTRATION,
        NEW_FUNCTIONS + MARKER_BEFORE_REGISTRATION,
        1,
    )
    text = text.replace(MARKER_TOOLS_LAST_LINE, NEW_TOOLS_LIST, 1)

    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Next: run register_tools.py + attach_tools_to_agents.py")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
