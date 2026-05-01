"""HTML генератор — MD → HTML через markdown-it-py."""
from pathlib import Path
from markdown_it import MarkdownIt

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 800px;
            margin: 2em auto; padding: 0 1em; line-height: 1.6; color: #222; }}
    h1, h2, h3 {{ color: #1a1a1a; }}
    code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px;
            font-family: Consolas, Monaco, monospace; font-size: 0.9em; }}
    pre {{ background: #f4f4f4; padding: 1em; border-radius: 5px; overflow-x: auto; }}
    pre code {{ background: none; padding: 0; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
    th {{ background: #f8f8f8; }}
    blockquote {{ border-left: 4px solid #ddd; padding-left: 1em; color: #666;
                  margin: 1em 0; }}
    a {{ color: #0066cc; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def generate_html(out_path: Path, content_md: str | None = None,
                  content_json: str | None = None, title: str | None = None) -> None:
    """Создать .html файл из markdown-контента."""
    if not content_md:
        raise ValueError("content_md is required for html format")
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    md.enable("table")
    body = md.render(content_md)
    out_path.write_text(
        _HTML_TEMPLATE.format(title=title or "Document", body=body),
        encoding="utf-8",
    )
