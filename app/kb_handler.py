"""
v0.8 Personal KB through Matrix chat.

Этот модуль реализует:
1. Обработку прикладывания файлов в чат (m.file/m.image/etc) → ingest в personal KB
2. Slash-команды /kb_help, /kb_list, /kb_delete <name>, /kb_clear, /kb_clear_confirm

Подключается к Bridge тремя точками:
1. В __init__: регистрируется callback на RoomMessageMedia (см. patch_bridge.txt)
2. В _on_message: проверка на slash-команду до отправки в Letta (см. patch_bridge.txt)
3. Этот модуль вызывается из bridge — все методы async и принимают bridge instance

Зависимости — только то что уже есть в bridge venv.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from nio import (
    DownloadResponse,
    MatrixRoom,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageAudio,
    RoomMessageVideo,
    RoomEncryptedFile,
    RoomEncryptedImage,
    RoomEncryptedAudio,
    RoomEncryptedVideo,
)
from nio.crypto.attachments import decrypt_attachment

if TYPE_CHECKING:
    from app.bridge import Bridge

log = logging.getLogger("kb_handler")

# === Конфиг ===
TMP_DIR = Path("/opt/ai/bridge/tmp/uploads")
TMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

INGEST_PYTHON = "/opt/ai/ingest/venv/bin/python"
INGEST_SCRIPT = "/opt/ai/ingest/ingest.py"

INTERNAL_API_URL = "http://127.0.0.1:8284"

MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".md", ".markdown", ".txt", ".xlsx", ".csv", ".json"}

INGEST_TIMEOUT_SEC = 120  # max время одного ingest

# State для двухшагового /kb_clear (в памяти процесса)
# {mxid: expires_at_unix_ts}
_clear_pending: dict[str, float] = {}
CLEAR_CONFIRM_TTL = 60  # секунд


# ============================================================
# File handler
# ============================================================

async def handle_file_message(
    bridge: "Bridge",
    room: MatrixRoom,
    event,
) -> None:
    """
    Обработать прикладывание файла в чат с Leo.

    Поддерживает и открытые (RoomMessage*), и зашифрованные (RoomEncrypted*) attachments.
    """
    if event.sender == bridge.cfg.user_id:
        return
    if bridge.startup_token is None:
        return
    if not bridge._is_allowed_user(event.sender):
        log.warning("File from disallowed user ignored: %s", event.sender)
        await bridge.client.room_leave(room.room_id)
        return

    # Определяем — это документ или картинка/видео/аудио
    is_file = isinstance(event, (RoomMessageFile, RoomEncryptedFile))
    is_media_other = isinstance(event, (
        RoomMessageImage, RoomMessageAudio, RoomMessageVideo,
        RoomEncryptedImage, RoomEncryptedAudio, RoomEncryptedVideo,
    ))
    is_encrypted = isinstance(event, (
        RoomEncryptedFile, RoomEncryptedImage, RoomEncryptedAudio, RoomEncryptedVideo,
    ))

    body = getattr(event, "body", "") or "(file)"
    mxc_url = getattr(event, "url", None)

    if not mxc_url or not mxc_url.startswith("mxc://"):
        log.info("File event without mxc:// url, skipping: %s", room.room_id)
        return

    # Картинки/аудио/видео — отказ
    if is_media_other:
        await _send_reply(bridge, room, event,
            f"Я работаю с документами (PDF, DOCX, MD, TXT, XLSX, CSV, JSON), "
            f"а это {_event_type_label(event)}. Не понимаю что с этим делать."
        )
        return

    if not is_file:
        log.info("Unsupported event type: %s", type(event).__name__)
        return

    # Проверка расширения
    ext = Path(body).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(e.lstrip(".") for e in SUPPORTED_EXTENSIONS))
        await _send_reply(bridge, room, event,
            f"❌ Не могу обработать «{body}» — поддерживаются только: {supported}."
        )
        return

    # Размер (info может отсутствовать у encrypted)
    info = getattr(event, "info", None)
    size = None
    if isinstance(info, dict):
        size = info.get("size")
    if size and size > MAX_FILE_SIZE:
        await _send_reply(bridge, room, event,
            f"❌ Файл «{body}» слишком большой ({_human_size(size)} > {MAX_FILE_SIZE_MB} MB лимит)."
        )
        return

    log.info("[%s] File from %s: %s (%s, encrypted=%s)",
             room.room_id[:12], event.sender, body, mxc_url, is_encrypted)
    await bridge.client.room_typing(room.room_id, typing_state=True, timeout=30000)

    try:
        # Скачиваем (с расшифровкой если encrypted)
        tmp_path = await _download_to_tmp(bridge, event, mxc_url, body, is_encrypted)
        if tmp_path is None:
            await _send_reply(bridge, room, event,
                f"❌ Не смог получить «{body}» из Matrix. Попробуй ещё раз."
            )
            return

        actual_size = tmp_path.stat().st_size
        if actual_size > MAX_FILE_SIZE:
            tmp_path.unlink(missing_ok=True)
            await _send_reply(bridge, room, event,
                f"❌ Файл «{body}» слишком большой ({_human_size(actual_size)} > {MAX_FILE_SIZE_MB} MB лимит)."
            )
            return

        result = await _run_ingest(
            tmp_path=tmp_path,
            user_mxid=event.sender,
            source_name=body,
        )
        tmp_path.unlink(missing_ok=True)

        if result.get("ok"):
            chunks = result.get("chunks", 0)
            await _send_reply(bridge, room, event,
                f"✅ Загрузил **«{body}»** в твою личную базу.\n"
                f"Фрагментов: {chunks}. Теперь могу искать по содержимому.\n\n"
                f"Попробуй: «что в {body} написано про…»"
            )
        else:
            err = result.get("error", "неизвестная ошибка")
            log.error("Ingest failed for %s: %s", body, err)
            await _send_reply(bridge, room, event,
                f"❌ Что-то пошло не так при обработке «{body}». "
                f"Попробуй ещё раз или обратись к администратору."
            )

    except Exception as e:
        log.exception("File handler crashed: %s", e)
        await _send_reply(bridge, room, event,
            f"❌ Внутренняя ошибка при обработке файла. Попробуй позже."
        )
    finally:
        await bridge.client.room_typing(room.room_id, typing_state=False)


# ============================================================
# Slash commands
# ============================================================

# Возвращает True если сообщение было slash-командой и обработано
async def try_handle_slash(
    bridge: "Bridge", room: MatrixRoom, event, clean_text: str
) -> bool:
    """
    Проверяет clean_text на slash-команду и обрабатывает её.
    Возвращает True если команда была обработана (тогда не отправляем в Letta).
    """
    text = clean_text.strip()
    if not text.startswith("/kb_"):
        return False

    parts = text.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    log.info("[%s] Slash from %s: %s", room.room_id[:12], event.sender, cmd)

    if cmd == "/kb_help":
        await _cmd_help(bridge, room, event)
    elif cmd == "/kb_list":
        await _cmd_list(bridge, room, event)
    elif cmd == "/kb_search":
        await _cmd_search(bridge, room, event, arg)
    elif cmd == "/kb_info":
        await _cmd_info(bridge, room, event, arg)
    elif cmd == "/kb_delete":
        await _cmd_delete(bridge, room, event, arg)
    elif cmd == "/kb_clear":
        await _cmd_clear(bridge, room, event)
    elif cmd == "/kb_clear_confirm":
        await _cmd_clear_confirm(bridge, room, event)
    else:
        await _send_reply(bridge, room, event,
            f"Неизвестная команда `{cmd}`. Напиши /kb_help."
        )
    return True


async def _cmd_help(bridge, room, event) -> None:
    msg = (
        "📚 **База знаний Leo — личная**\n"
        "\n"
        "**Что умею:**\n"
        "\n"
        "- Запоминать твои документы (PDF, DOCX, MD, TXT, XLSX, CSV, JSON — до 10 MB)\n"
        "- Искать по содержимому когда ты спрашиваешь\n"
        "- Видишь только ты — другие пользователи к ним не имеют доступа\n"
        "\n"
        "**Как загрузить:**\n"
        "\n"
        "- Просто **приложи файл прямо в этот чат** — я обработаю и запомню\n"
        "- Файл с тем же именем заменит предыдущую версию\n"
        "\n"
        "**Команды:**\n"
        "\n"
        "- `/kb_list` — список моих документов\n"
        "- `/kb_search <запрос>` — найти по содержимому (например: `/kb_search архитектура`)\n"
        "- `/kb_info <имя>` — детали документа (размер, дата, фрагменты)\n"
        "- `/kb_delete <имя>` — удалить документ (например: `/kb_delete notes.pdf`)\n"
        "- `/kb_clear` — снести всё (с подтверждением)\n"
        "- `/kb_help` — это сообщение\n"
        "\n"
        "**Или просто скажи мне:**\n"
        "\n"
        "- «найди в моих заметках про X» — я сам поищу\n"
        "- «что у меня загружено?» — покажу список\n"
        "- «забудь файл X» — удалю"
    )
    await _send_reply(bridge, room, event, msg)


async def _cmd_list(bridge, room, event) -> None:
    data = await _api_post("/kb/personal/list", {"matrix_user_id": event.sender})
    if data is None:
        await _send_reply(bridge, room, event, "❌ Не смог получить список (ошибка API).")
        return

    docs = data.get("documents", [])
    if not docs:
        await _send_reply(bridge, room, event,
            "📚 Твоя личная база пуста. Приложи файл, чтобы начать."
        )
        return

    lines = [f"📚 **В твоей KB:**\n"]
    for d in docs:
        last = d.get("last_added", "")
        when = _humanize_iso(last) if last else "?"
        lines.append(f"• **{d['source']}** — {d['chunks']} фрагментов, добавлено {when}")
    lines.append(f"\n_Всего: {data.get('count', 0)} документов, "
                 f"{data.get('total_chunks', 0)} фрагментов_")

    await _send_reply(bridge, room, event, "\n".join(lines))


async def _cmd_delete(bridge, room, event, arg: str) -> None:
    if not arg:
        await _send_reply(bridge, room, event,
            "Укажи имя документа: `/kb_delete notes.pdf`. "
            "Список документов: `/kb_list`."
        )
        return

    data = await _api_post(
        "/kb/personal/delete",
        {"matrix_user_id": event.sender, "source": arg},
    )
    if data is None:
        await _send_reply(bridge, room, event, "❌ Ошибка при удалении (API).")
        return

    deleted = data.get("deleted", 0)
    if deleted == 0:
        await _send_reply(bridge, room, event,
            f"Документа «{arg}» в твоей базе нет. Список: `/kb_list`."
        )
        return

    matched = data.get("matched_source", arg)
    await _send_reply(bridge, room, event,
        f"✅ Удалил «{matched}» — {deleted} фрагментов из твоей KB."
    )


async def _cmd_clear(bridge, room, event) -> None:
    """Шаг 1: запросить подтверждение."""
    # Сначала узнаем сколько у пользователя в KB
    data = await _api_post("/kb/personal/list", {"matrix_user_id": event.sender})
    total_docs = data.get("count", 0) if data else 0
    total_chunks = data.get("total_chunks", 0) if data else 0

    if total_docs == 0:
        await _send_reply(bridge, room, event, "📚 Твоя база уже пуста.")
        return

    _clear_pending[event.sender] = time.time() + CLEAR_CONFIRM_TTL
    await _send_reply(bridge, room, event,
        f"⚠️ Это удалит **ВСЕ** твои документы ({total_docs} файлов, {total_chunks} фрагментов).\n"
        f"Чтобы подтвердить — напиши `/kb_clear_confirm` в течение {CLEAR_CONFIRM_TTL} секунд."
    )


async def _cmd_clear_confirm(bridge, room, event) -> None:
    """Шаг 2: реально очистить."""
    expires = _clear_pending.get(event.sender)
    if not expires or time.time() > expires:
        _clear_pending.pop(event.sender, None)
        await _send_reply(bridge, room, event,
            "❌ Подтверждение истекло (или не запрошено). Сначала отправь `/kb_clear`."
        )
        return

    _clear_pending.pop(event.sender, None)
    data = await _api_post("/kb/personal/clear", {"matrix_user_id": event.sender})
    if data is None:
        await _send_reply(bridge, room, event, "❌ Ошибка при очистке (API).")
        return

    deleted = data.get("deleted", 0)
    await _send_reply(bridge, room, event,
        f"✅ Очищено. Удалено фрагментов: {deleted}. В твоей KB больше ничего нет."
    )


async def _cmd_search(bridge, room, event, arg: str) -> None:
    """Прямой семантический поиск без обращения к LLM."""
    if not arg:
        await _send_reply(bridge, room, event,
            "Укажи запрос: `/kb_search архитектура`. "
            "Список документов: `/kb_list`."
        )
        return

    # Используем существующий kb_search_personal endpoint
    data = await _api_post(
        "/kb/search_personal",
        {"matrix_user_id": event.sender, "query": arg, "limit": 5},
    )
    if data is None:
        await _send_reply(bridge, room, event, "❌ Ошибка при поиске (API).")
        return

    formatted = data.get("formatted", "")
    if not formatted:
        await _send_reply(bridge, room, event,
            f"По запросу «{arg}» в твоей KB ничего не нашлось."
        )
        return

    await _send_reply(bridge, room, event,
        f"🔍 Поиск по «{arg}»:\n\n{formatted}"
    )


async def _cmd_info(bridge, room, event, arg: str) -> None:
    """Детали одного документа."""
    if not arg:
        await _send_reply(bridge, room, event,
            "Укажи имя документа: `/kb_info notes.pdf`. "
            "Список: `/kb_list`."
        )
        return

    data = await _api_post(
        "/kb/personal/info",
        {"matrix_user_id": event.sender, "source": arg},
    )
    if data is None:
        await _send_reply(bridge, room, event, "❌ Ошибка API.")
        return

    if not data.get("found"):
        await _send_reply(bridge, room, event,
            f"Документа «{arg}» в твоей KB нет. Список: `/kb_list`."
        )
        return

    sha = data.get("sha256", "")
    sha_short = sha[:8] + "..." if sha else "?"
    first = data.get("first_added", "")
    last = data.get("last_added", "")
    when_first = _humanize_iso(first) if first else "?"
    when_last = _humanize_iso(last) if last else "?"
    chars = data.get("total_chars", 0)
    chunks = data.get("chunks", 0)

    info_text = (
        f"📄 **{data.get('source')}**\n"
        f"\n"
        f"- Фрагментов: **{chunks}**\n"
        f"- Размер текста: ~{chars:,} символов\n"
        f"- sha256: `{sha_short}`\n"
        f"- Первая загрузка: {when_first}\n"
        f"- Последняя: {when_last}"
    )
    await _send_reply(bridge, room, event, info_text)


# ============================================================
# Helpers
# ============================================================

async def _download_to_tmp(bridge, event, mxc_url: str, original_name: str, is_encrypted: bool) -> Path | None:
    """Скачать (и опционально расшифровать) mxc:// → файл в /tmp."""
    parts = mxc_url[len("mxc://"):].split("/", 1)
    if len(parts) != 2:
        log.error("Bad mxc URL: %s", mxc_url)
        return None
    server_name, media_id = parts

    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in original_name)
    safe_name = safe_name[:120] or "uploaded"
    suffix = Path(original_name).suffix.lower()
    if suffix and not safe_name.endswith(suffix):
        safe_name += suffix

    tmp_path = TMP_DIR / f"{int(time.time() * 1000)}_{safe_name}"

    resp = await bridge.client.download(server_name=server_name, media_id=media_id)
    if not isinstance(resp, DownloadResponse):
        log.error("Matrix download failed: %r", resp)
        return None

    body = resp.body
    if is_encrypted:
        # Encrypted attachment — расшифровываем через nio.crypto.attachments
        try:
            body = decrypt_attachment(
                body,
                event.key["k"],
                event.hashes["sha256"],
                event.iv,
            )
        except Exception as e:
            log.exception("decrypt_attachment failed: %s", e)
            return None
        log.info("Decrypted attachment: %d bytes (encrypted) -> %d bytes (plain)",
                 len(resp.body), len(body))

    tmp_path.write_bytes(body)
    log.info("Downloaded %s → %s (%d bytes, encrypted=%s)",
             mxc_url, tmp_path, len(body), is_encrypted)
    return tmp_path


async def _run_ingest(tmp_path: Path, user_mxid: str, source_name: str) -> dict:
    """Запуск ingest.py как subprocess. Возвращает распарсенный JSON-результат."""
    cmd = [
        INGEST_PYTHON,
        INGEST_SCRIPT,
        "--source", str(tmp_path),
        "--user", user_mxid,
        "--source-name", source_name,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=INGEST_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": "ingest timeout"}

        if proc.returncode != 0:
            log.error("Ingest non-zero exit %d: stderr=%s",
                      proc.returncode, stderr.decode("utf-8", "replace")[:500])
            return {"ok": False, "error": f"exit {proc.returncode}"}

        # v1.0.0a: find JSON summary line (was: last line, but ingest now logs
        # an extra '=== DONE ===' after the JSON output).
        text = stdout.decode("utf-8", "replace").strip()
        json_line = None
        for line in reversed(text.splitlines()):
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                json_line = stripped
                break
        if json_line is None:
            log.error("Ingest stdout has no JSON summary, full output: %s", text[:1000])
            return {"ok": False, "error": "no json summary"}
        try:
            return json.loads(json_line)
        except Exception as e:
            log.error("Ingest JSON parse failed (%s), line: %s", e, json_line[:300])
            return {"ok": False, "error": f"json parse error: {e}"}
    except Exception as e:
        log.exception("Subprocess error: %s", e)
        return {"ok": False, "error": str(e)}


async def _api_post(path: str, body: dict) -> dict | None:
    """POST на bridge internal API. Возвращает None при ошибке."""
    token = os.environ.get("BRIDGE_INTERNAL_TOKEN", "")
    if not token:
        log.error("BRIDGE_INTERNAL_TOKEN not set")
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{INTERNAL_API_URL}{path}",
                headers={"X-Internal-Token": token, "Content-Type": "application/json"},
                json=body,
            )
        if r.status_code >= 400:
            log.error("Internal API %s -> %d: %s", path, r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        log.exception("Internal API call failed: %s", e)
        return None


async def _send_reply(bridge, room: MatrixRoom, event, text: str) -> None:
    """Отправить ответ в тред (как _on_message в bridge)."""
    # Ленивый импорт чтобы избежать циклической зависимости
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})

    thread_root = bridge._get_thread_root(event)
    html_body = md.render(text)
    content = {
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": html_body,
    }
    # v1.3.1: тред только в групповых комнатах ИЛИ если юзер сам в треде
    is_dm = len(room.users) <= 2
    user_in_thread = (event.source.get("content", {})
                      .get("m.relates_to", {})
                      .get("rel_type") == "m.thread")
    if not is_dm or user_in_thread:
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_root,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": event.event_id},
        }
    await bridge.client.room_send(
        room.room_id,
        message_type="m.room.message",
        content=content,
        ignore_unverified_devices=True,
    )


def _event_type_label(event) -> str:
    if isinstance(event, RoomMessageImage):
        return "картинка"
    if isinstance(event, RoomMessageAudio):
        return "аудио"
    if isinstance(event, RoomMessageVideo):
        return "видео"
    return "файл такого типа"


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/(1024*1024):.1f} MB"


def _humanize_iso(iso_str: str) -> str:
    """ISO datetime → '2 часа назад' / 'вчера' / 'дата'."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = (now - dt).total_seconds()
        if diff < 60:
            return "только что"
        if diff < 3600:
            return f"{int(diff/60)} мин назад"
        if diff < 86400:
            return f"{int(diff/3600)} ч назад"
        if diff < 86400 * 7:
            return f"{int(diff/86400)} дн назад"
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso_str
