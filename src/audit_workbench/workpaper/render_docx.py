"""Render the workpaper document model as a Word file (needs the extra `workpaper`).

Real Word headings and tables, so the auditor can complete sections 6 to 8 in Word.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from docx import Document as new_document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import Table as WordTable
from docx.text.paragraph import Paragraph as WordParagraph

from audit_workbench.workpaper.model import Document, Heading, Paragraph, Table

_PAGE_WIDTH_CM = 21.0
_PAGE_HEIGHT_CM = 29.7
_MARGIN_CM = 2.0
_TEXT_WIDTH_CM = _PAGE_WIDTH_CM - 2 * _MARGIN_CM
_MONO = "Courier New"
_HEADER_FILL = "D9E2F3"


def _field(paragraph: WordParagraph, instruction: str) -> None:
    """Insert a Word field (e.g. PAGE) with a placeholder result."""
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, instruction), ("separate", None), (None, "1")):
        if kind is not None:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), kind)
        elif text == instruction:
            element = OxmlElement("w:instrText")
            element.set(qn("xml:space"), "preserve")
            element.text = f" {instruction} "
        else:
            element = OxmlElement("w:t")
            element.text = text
        run._r.append(element)
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(end)


def _set_mono(run, size: float) -> None:
    run.font.name = _MONO
    run.font.size = Pt(size)
    run._r.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), _MONO)


def _shade(cell, fill: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    properties.append(shading)


def _repeat_header(row) -> None:
    properties = row._tr.get_or_add_trPr()
    element = OxmlElement("w:tblHeader")
    element.set(qn("w:val"), "true")
    properties.append(element)


def _no_split(row) -> None:
    row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))


def _write_cell(cell, text: str, *, mono: bool, bold: bool = False) -> None:
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run(text)
    run.bold = bold
    if mono:
        _set_mono(run, 8)
    else:
        run.font.size = Pt(9)


def _add_table(doc, block: Table) -> WordTable:
    table = doc.add_table(rows=1, cols=len(block.headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    total = sum(block.widths) if block.widths else len(block.headers)
    widths = [
        Cm(_TEXT_WIDTH_CM * (block.widths[i] if block.widths else 1) / total)
        for i in range(len(block.headers))
    ]
    header = table.rows[0]
    _repeat_header(header)
    for cell, text in zip(header.cells, block.headers, strict=True):
        _write_cell(cell, text, mono=False, bold=True)
        _shade(cell, _HEADER_FILL)
    for values in block.rows:
        row = table.add_row()
        _no_split(row)
        for index, (cell, text) in enumerate(zip(row.cells, values, strict=True)):
            _write_cell(cell, text, mono=index in block.mono_columns)
    for row in table.rows:
        for cell, width in zip(row.cells, widths, strict=True):
            cell.width = width
    return table


def _add_paragraph(doc, block: Paragraph) -> None:
    paragraph = doc.add_paragraph()
    if block.style == "mono":
        paragraph.paragraph_format.space_after = Pt(8)
        paragraph.paragraph_format.space_before = Pt(4)
        _set_mono(paragraph.add_run(block.text), 8)
    elif block.style == "note":
        run = paragraph.add_run(block.text)
        run.italic = True
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    else:
        paragraph.add_run(block.text)


def render_docx(document: Document, path: Path | str) -> Path:
    """Write `document` to `path` as DOCX and return the path."""
    path = Path(path)
    doc = new_document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(_PAGE_WIDTH_CM), Cm(_PAGE_HEIGHT_CM)
    section.left_margin = section.right_margin = Cm(_MARGIN_CM)
    section.top_margin = section.bottom_margin = Cm(_MARGIN_CM)
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10)

    stamp = datetime(document.run_date.year, document.run_date.month, document.run_date.day)
    props = doc.core_properties
    props.title = document.title
    props.author = document.author
    props.last_modified_by = document.author
    props.subject = "Audit workpaper (ISA 230)"
    props.comments = document.tool
    props.created = props.modified = props.last_printed = stamp

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = footer.add_run(document.footer_text + "    Page ")
    run.font.size = Pt(7)
    _field(footer, "PAGE")
    for r in footer.runs:
        r.font.size = Pt(7)

    doc.add_heading(document.title, level=0)
    for part in document.sections:
        doc.add_heading(part.heading, level=1)
        for block in part.blocks:
            if isinstance(block, Heading):
                doc.add_heading(block.text, level=block.level)
            elif isinstance(block, Paragraph):
                _add_paragraph(doc, block)
            else:
                _add_table(doc, block)
                doc.add_paragraph().paragraph_format.space_after = Pt(2)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
