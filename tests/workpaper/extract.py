"""Read the text back out of a rendered workpaper. Tests compare text, never file bytes."""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document as OpenDocx
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


def squash(text: str) -> str:
    """Whitespace collapsed to single spaces, as a reader sees it."""
    return re.sub(r"\s+", " ", text).strip()


def docx_texts(path: Path) -> list[str]:
    """Paragraph and table cell texts of the body, in reading order."""
    doc = OpenDocx(str(path))
    out: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            out.append(Paragraph(child, doc).text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                out += [cell.text for cell in row.cells]
    return out


def docx_text(path: Path) -> str:
    return "\n".join(docx_texts(path))


def pdf_pages(path: Path) -> list[str]:
    return [page.extract_text() or "" for page in PdfReader(str(path)).pages]


def pdf_text(path: Path) -> str:
    return "\n".join(pdf_pages(path))
