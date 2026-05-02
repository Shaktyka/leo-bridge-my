#!/usr/bin/env python3
"""
v1.7.0 patcher: добавляет в /opt/ai/bridge/app/internal_api.py:
- POST /templates/list  → список доступных шаблонов
- POST /templates/render → рендер шаблона + отправка в Matrix через bridge

Идемпотентен.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/app/internal_api.py")
BACKUP = Path("/opt/ai/bridge/app/internal_api.py.bak-pre-v170")


# Маркер: вставляем перед def serve()
MARKER_BEFORE_SERVE = "def serve() -> None:"

INSERT_BEFORE_SERVE = '''
# =============================================================================
# v1.7.0: Templates
# =============================================================================
class TemplatesListReq(BaseModel):
    pass  # без параметров


class TemplatesRenderReq(BaseModel):
    name: str = Field(..., description="Имя шаблона из реестра")
    params: dict[str, Any] = Field(default_factory=dict)
    matrix_room_id: str = Field(..., description="Куда отправить готовый файл")
    matrix_user_id: str = Field(..., description="MXID юзера для ACL")


@app.post("/templates/list")
async def templates_list(_: None = Depends(check_auth)) -> dict[str, Any]:
    """v1.7.0: список доступных шаблонов."""
    from app.templates import list_templates
    return {"templates": list_templates()}


@app.post("/templates/render")
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
    }


'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "@app.post(\"/templates/render\")" in text or "/templates/render" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if MARKER_BEFORE_SERVE not in text:
        print("ERROR: serve() marker not found", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(
        MARKER_BEFORE_SERVE,
        INSERT_BEFORE_SERVE + MARKER_BEFORE_SERVE,
        1,
    )

    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Run: sudo systemctl restart bridge-internal-api")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
