"""Render the workpaper document model as a PDF (needs the extra `workpaper`).

A4, built-in fonts (so no font files are needed and it runs in CI), page numbers and the
data hash of the run in the footer of every page. The built-in fonts cover WinAnsi
(cp1252) only: other characters are replaced by `?`; `Document.characters_outside` tells
which ones those are.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph as PdfParagraph,
)
from reportlab.platypus import (
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)
from reportlab.platypus import (
    Table as PdfTable,
)

from audit_workbench.workpaper.model import (
    Document,
    Heading,
    Paragraph,
    Table,
    replace_unsupported,
)

_MARGIN = 2 * cm
_BOTTOM = 2.3 * cm
_TEXT_WIDTH = A4[0] - 2 * _MARGIN
_MONO_SIZE = 7.5
_MONO_CHARS = int(_TEXT_WIDTH / (0.6 * _MONO_SIZE)) - 1  # Courier is 0.6 em wide
_HEADER_FILL = colors.HexColor("#D9E2F3")
_GRID = colors.HexColor("#888888")


def wrap_mono(text: str, width: int) -> list[str]:
    """Wrap fixed-width text to `width` characters; continuation lines are indented by 4."""
    out: list[str] = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        indent = " " * (len(line) - len(line.lstrip(" ")))
        out += textwrap.wrap(
            line,
            width,
            initial_indent="",
            subsequent_indent=indent + "    ",
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        )
    return out


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "wpTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=22, spaceAfter=14
        ),
        "h1": ParagraphStyle(
            "wpH1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=15,
            spaceBefore=16,
            spaceAfter=8,
            keepWithNext=1,
        ),
        "h2": ParagraphStyle(
            "wpH2",
            parent=base["Heading2"],
            fontName="Courier-Bold",
            fontSize=9.5,
            spaceBefore=10,
            spaceAfter=4,
            keepWithNext=1,
        ),
        "body": ParagraphStyle(
            "wpBody", parent=base["BodyText"], fontName="Helvetica", fontSize=9.5, leading=12.5
        ),
        "note": ParagraphStyle(
            "wpNote",
            parent=base["BodyText"],
            fontName="Helvetica-Oblique",
            fontSize=8,
            leading=10.5,
            textColor=colors.HexColor("#555555"),
        ),
        "cell": ParagraphStyle(
            "wpCell", parent=base["BodyText"], fontName="Helvetica", fontSize=8, leading=10
        ),
        "cell_head": ParagraphStyle(
            "wpCellHead",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
        ),
        "cell_mono": ParagraphStyle(
            "wpCellMono", parent=base["BodyText"], fontName="Courier", fontSize=7, leading=9
        ),
        "mono": ParagraphStyle(
            "wpMono", fontName="Courier", fontSize=_MONO_SIZE, leading=_MONO_SIZE + 1.5
        ),
    }


def _markup(text: str) -> str:
    """Plain text as Paragraph markup: replaced characters, escaped, newlines as <br/>."""
    return escape(replace_unsupported(text)).replace("\n", "<br/>")


def _table(block: Table, styles: dict[str, ParagraphStyle]) -> PdfTable:
    total = sum(block.widths) if block.widths else len(block.headers)
    fractions = block.widths or (1,) * len(block.headers)
    col_widths = [_TEXT_WIDTH * w / total for w in fractions]
    data = [[PdfParagraph(_markup(h), styles["cell_head"]) for h in block.headers]]
    for row in block.rows:
        data.append(
            [
                PdfParagraph(
                    _markup(cell), styles["cell_mono" if i in block.mono_columns else "cell"]
                )
                for i, cell in enumerate(row)
            ]
        )
    table = PdfTable(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
                ("BACKGROUND", (0, 0), (-1, 0), _HEADER_FILL),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _footer(document: Document):
    footer_text = replace_unsupported(document.footer_text)
    label = replace_unsupported(f"{document.title} - {document.tool} - run {document.run_date}")

    def draw(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Courier", 6.3)
        canvas.drawString(_MARGIN, 1.35 * cm, footer_text)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(_MARGIN, 0.9 * cm, label)
        canvas.drawRightString(A4[0] - _MARGIN, 0.9 * cm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    return draw


def render_pdf(document: Document, path: Path | str) -> Path:
    """Write `document` to `path` as PDF and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = _styles()
    story: list = [PdfParagraph(_markup(document.title), styles["title"])]
    for part in document.sections:
        story.append(PdfParagraph(_markup(part.heading), styles["h1"]))
        for block in part.blocks:
            if isinstance(block, Heading):
                story.append(PdfParagraph(_markup(block.text), styles["h2"]))
            elif isinstance(block, Paragraph):
                if block.style == "mono":
                    lines = wrap_mono(replace_unsupported(block.text), _MONO_CHARS)
                    story.append(Preformatted("\n".join(lines), styles["mono"]))
                    story.append(Spacer(1, 6))
                else:
                    story.append(PdfParagraph(_markup(block.text), styles[block.style]))
                    story.append(Spacer(1, 4))
            else:
                story.append(_table(block, styles))
                story.append(Spacer(1, 6))
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_BOTTOM,
        title=replace_unsupported(document.title),
        author=replace_unsupported(document.author),
        subject="Audit workpaper (ISA 230)",
        creator=document.tool,
        invariant=1,
    )
    draw = _footer(document)
    doc.build(story, onFirstPage=draw, onLaterPages=draw)
    return path
