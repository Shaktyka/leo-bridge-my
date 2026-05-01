"""MD генератор — просто записывает контент в файл."""
from pathlib import Path


def generate_md(out_path: Path, content_md: str | None = None,
                content_json: str | None = None, title: str | None = None) -> None:
    """Создать .md файл из markdown-контента."""
    if not content_md:
        raise ValueError("content_md is required for md format")
    text = ""
    if title:
        text += f"# {title}\n\n"
    text += content_md
    out_path.write_text(text, encoding="utf-8")
