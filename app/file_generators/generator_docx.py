"""DOCX генератор — MD через pandoc."""
import logging
import tempfile
from pathlib import Path
import pypandoc

log = logging.getLogger(__name__)


def generate_docx(out_path: Path, content_md: str | None = None,
                  content_json: str | None = None, title: str | None = None) -> None:
    """Создать .docx через pandoc."""
    if not content_md:
        raise ValueError("content_md is required for docx format")

    text = ""
    if title:
        text += f"# {title}\n\n"
    text += content_md

    # Временный файл в /tmp (где ai точно может писать)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8", dir="/tmp"
    ) as tmp:
        tmp.write(text)
        tmp_md = tmp.name

    try:
        pypandoc.convert_file(
            tmp_md, "docx",
            outputfile=str(out_path),
            format="markdown",
            extra_args=["--standalone"],
        )
        log.info("docx generated: %s (%d bytes)", out_path, out_path.stat().st_size)
    finally:
        Path(tmp_md).unlink(missing_ok=True)
