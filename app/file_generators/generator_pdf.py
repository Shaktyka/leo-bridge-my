"""PDF генератор — MD через pandoc + weasyprint."""
import logging
import tempfile
from pathlib import Path
from weasyprint import HTML
from markdown_it import MarkdownIt

log = logging.getLogger(__name__)

_PDF_CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: -apple-system, system-ui, "Helvetica", sans-serif;
       font-size: 11pt; line-height: 1.5; color: #222; }
h1 { font-size: 22pt; color: #1a1a1a; margin-top: 0.5em; }
h2 { font-size: 16pt; color: #1a1a1a; }
h3 { font-size: 13pt; }
code { background: #f4f4f4; padding: 2px 4px; border-radius: 3px;
       font-family: Consolas, monospace; font-size: 0.9em; }
pre { background: #f4f4f4; padding: 0.8em; border-radius: 4px;
      overflow-x: auto; font-size: 0.85em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #ccc; padding: 6px; text-align: left; }
th { background: #f0f0f0; }
blockquote { border-left: 3px solid #ccc; padding-left: 1em; color: #555; }
a { color: #0066cc; }
"""


def generate_pdf(out_path: Path, content_md: str | None = None,
                 content_json: str | None = None, title: str | None = None) -> None:
    """Создать .pdf через MD → HTML → WeasyPrint."""
    if not content_md:
        raise ValueError("content_md is required for pdf format")

    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    md.enable("table")

    text = ""
    if title:
        text += f"# {title}\n\n"
    text += content_md

    body = md.render(text)
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<title>{title or "Document"}</title>
<style>{_PDF_CSS}</style></head>
<body>{body}</body></html>"""

    HTML(string=html).write_pdf(str(out_path))
    log.info("pdf generated: %s (%d bytes)", out_path, out_path.stat().st_size)
