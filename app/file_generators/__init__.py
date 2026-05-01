"""
v1.5.0: генераторы файлов по форматам.

Поддерживаемые форматы:
- md      — обычный markdown
- html    — markdown-it конвертер
- docx    — pandoc
- pdf     — pandoc + weasyprint
- xlsx    — openpyxl + JSON content
- pptx    — python-pptx + JSON content
"""
from .generator_md import generate_md
from .generator_html import generate_html
from .generator_docx import generate_docx
from .generator_pdf import generate_pdf
from .generator_xlsx import generate_xlsx
from .generator_pptx import generate_pptx

GENERATORS = {
    "md":   generate_md,
    "html": generate_html,
    "docx": generate_docx,
    "pdf":  generate_pdf,
    "xlsx": generate_xlsx,
    "pptx": generate_pptx,
}
