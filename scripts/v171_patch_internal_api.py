#!/usr/bin/env python3
"""
v1.7.1 patcher: модифицирует /templates/render endpoint в internal_api.py:
- Замер duration_ms от начала render до отправки файла.
- history_log в ai.report_history после каждого рендера.
- В ответ добавлено поле cache_hit.
- Новый endpoint POST /templates/history для аналитики.

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/app/internal_api.py")
BACKUP = Path("/opt/ai/bridge/app/internal_api.py.bak-pre-v171")

# Найдём блок templates_render и заменим его на расширенную версию.
# Маркер начала — наш v1.7.0 endpoint. Маркер конца — закрытие функции.

OLD = '''@app.post("/templates/render")
async def templates_render(
    req: TemplatesRenderReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """v1.7.0: отрендерить шаблон и отправить готовый файл в комнату.

    Поток:
    1. dispatcher → renderer возвращает {filename, format, content_md, title}
    2. POST на /files/create — генерация файла + отправка в Matrix через bridge
    3. Возврат event_id и метаданных
    """
    if state.pg_pool is None:
        raise HTTPException(status_code=503, detail="DB pool not ready")

    from app.templates import render as render_template

    try:
        rendered = await render_template(
            name=req.name,
            params=req.params,
            leo_pool=state.pg_pool,
            matrix_room_id=req.matrix_room_id,
            matrix_user_id=req.matrix_user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("template render failed: %s", e)
        raise HTTPException(status_code=500, detail=f"render failed: {e}")

    # Отправляем как файл через существующий /files/create endpoint
    import httpx
    payload = {
        "matrix_room_id": req.matrix_room_id,
        "creator_user_id": req.matrix_user_id,
        "filename": rendered["filename"],
        "format": rendered["format"],
        "content_md": rendered.get("content_md"),
        "title": rendered.get("title"),
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "http://127.0.0.1:8284/files/create",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json=payload,
        )
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"files/create failed: {r.status_code} {r.text[:300]}",
        )
    file_data = r.json()

    return {
        "ok": True,
        "template": req.name,
        "filename": file_data.get("filename"),
        "format": file_data.get("format"),
        "size_bytes": file_data.get("size_bytes"),
        "event_id": file_data.get("event_id"),
        "title": rendered.get("title"),
    }'''

NEW = '''@app.post("/templates/render")
async def templates_render(
    req: TemplatesRenderReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """v1.7.1: отрендерить шаблон, отправить файл, залогировать в history."""
    import time
    if state.pg_pool is None:
        raise HTTPException(status_code=503, detail="DB pool not ready")

    from app.templates import render as render_template
    from app.templates.base import history_log

    started = time.monotonic()
    error_message: str | None = None
    file_size: int | None = None
    cache_hit: bool = False
    rendered: dict | None = None
    status: str = "ok"

    try:
        try:
            rendered = await render_template(
                name=req.name,
                params=req.params,
                leo_pool=state.pg_pool,
                matrix_room_id=req.matrix_room_id,
                matrix_user_id=req.matrix_user_id,
            )
            cache_hit = bool(rendered.get("cache_hit", False))
        except ValueError as e:
            status = "error"
            error_message = str(e)
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            status = "error"
            error_message = f"{type(e).__name__}: {e}"
            log.exception("template render failed: %s", e)
            raise HTTPException(status_code=500, detail=f"render failed: {e}")

        # Отправка как файла через /files/create
        import httpx
        payload = {
            "matrix_room_id": req.matrix_room_id,
            "creator_user_id": req.matrix_user_id,
            "filename": rendered["filename"],
            "format": rendered["format"],
            "content_md": rendered.get("content_md"),
            "title": rendered.get("title"),
        }
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                "http://127.0.0.1:8284/files/create",
                headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
                json=payload,
            )
        if r.status_code >= 400:
            status = "error"
            error_message = f"files/create {r.status_code}"
            raise HTTPException(
                status_code=502,
                detail=f"files/create failed: {r.status_code} {r.text[:300]}",
            )
        file_data = r.json()
        file_size = file_data.get("size_bytes")

        # Empty doc detection: format=md без MD5 hash в filename
        if rendered.get("format") == "md" and "_empty" in (rendered.get("filename") or ""):
            status = "empty"

        return {
            "ok": True,
            "template": req.name,
            "filename": file_data.get("filename"),
            "format": file_data.get("format"),
            "size_bytes": file_size,
            "event_id": file_data.get("event_id"),
            "title": rendered.get("title"),
            "cache_hit": cache_hit,
        }
    finally:
        # Лог в history независимо от исхода
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            await history_log(
                leo_pool=state.pg_pool,
                template_name=req.name,
                params=req.params,
                matrix_user_id=req.matrix_user_id,
                matrix_room_id=req.matrix_room_id,
                duration_ms=duration_ms,
                status=status,
                file_size=file_size,
                error_message=error_message,
                cache_hit=cache_hit,
            )
        except Exception as e:
            log.warning("history_log skipped: %s", e)


# v1.7.1: история генераций
class TemplatesHistoryReq(BaseModel):
    template_name: str | None = Field(default=None, description="Фильтр по имени шаблона")
    matrix_user_id: str | None = Field(default=None, description="Фильтр по юзеру")
    limit: int = Field(default=50, ge=1, le=500)


@app.post("/templates/history")
async def templates_history(
    req: TemplatesHistoryReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """v1.7.1: вернуть последние записи из ai.report_history."""
    if state.pg_pool is None:
        raise HTTPException(status_code=503, detail="DB pool not ready")

    where: list[str] = []
    args: list[Any] = []
    i = 1
    if req.template_name:
        where.append(f"template_name = ${i}")
        args.append(req.template_name)
        i += 1
    if req.matrix_user_id:
        where.append(f"matrix_user_id = ${i}")
        args.append(req.matrix_user_id)
        i += 1

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, template_name, params_json, matrix_user_id, matrix_room_id,
               created_at, duration_ms, status, file_size, error_message, cache_hit
        FROM ai.report_history
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ${i}
    """
    args.append(req.limit)

    async with state.pg_pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    return {
        "count": len(rows),
        "items": [
            {
                "id": r["id"],
                "template_name": r["template_name"],
                "params": r["params_json"],
                "matrix_user_id": r["matrix_user_id"],
                "matrix_room_id": r["matrix_room_id"],
                "created_at": r["created_at"].isoformat(),
                "duration_ms": r["duration_ms"],
                "status": r["status"],
                "file_size": r["file_size"],
                "error_message": r["error_message"],
                "cache_hit": r["cache_hit"],
            }
            for r in rows
        ],
    }'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if '"/templates/history"' in text or "v1.7.1: отрендерить шаблон" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: v1.7.0 templates_render block not found in expected form", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Run: sudo systemctl restart bridge-internal-api")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
