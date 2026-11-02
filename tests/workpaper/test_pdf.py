"""The PDF renderer, checked by reading the file back with pypdf (spec section 7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pypdf import PdfReader

from audit_workbench.runner import Run
from audit_workbench.workpaper.model import Document, Heading, Paragraph, Section, Table
from audit_workbench.workpaper.render_pdf import render_pdf, wrap_mono

from .conftest import RUN_DATE
from .extract import pdf_pages, pdf_text, squash


def _nospace(text: str) -> str:
    return re.sub(r"\s+", "", text)


@pytest.fixture(scope="module")
def pdf_path(doc7: Document, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return render_pdf(doc7, tmp_path_factory.mktemp("pdf") / "workpaper.pdf")


def test_the_headings_of_all_eight_sections_are_found(doc7: Document, pdf_path: Path) -> None:
    text = squash(pdf_text(pdf_path))
    for section in doc7.sections:
        assert section.heading in text, section.heading
    positions = [text.index(s.heading) for s in doc7.sections]
    assert positions == sorted(positions)


def test_every_finding_id_is_found(run7: Run, pdf_path: Path) -> None:
    text = _nospace(pdf_text(pdf_path))
    missing = [f.finding_id for f in run7.findings if f.finding_id not in text]
    assert not missing


def test_every_page_has_the_data_hash_and_a_page_number(doc7: Document, pdf_path: Path) -> None:
    pages = pdf_pages(pdf_path)
    assert len(pages) > 5
    for number, page in enumerate(pages, start=1):
        assert doc7.data_hash in _nospace(page), f"page {number}"
        assert re.search(rf"Page\s*{number}\b", page), f"page {number}"


def test_the_page_is_a4(pdf_path: Path) -> None:
    box = PdfReader(str(pdf_path)).pages[0].mediabox
    assert (round(float(box.width)), round(float(box.height))) == (595, 842)


def test_every_input_hash_is_found_in_full(bank7: Path, pdf_path: Path) -> None:
    from audit_workbench.core.evidence import sha256_file

    text = _nospace(pdf_text(pdf_path))
    for path in bank7.glob("*.csv"):
        assert sha256_file(path) in text, path.name


def test_reviewer_fields_stay_empty_in_the_text(pdf_path: Path) -> None:
    text = squash(pdf_text(pdf_path))
    after = text[text.index("8. Sign-off") :]
    assert re.search(r"Reviewed by\s+Date of review\s+Extent of review", after)


def test_the_properties_are_set(doc7: Document, pdf_path: Path) -> None:
    meta = PdfReader(str(pdf_path)).metadata
    assert meta is not None
    assert meta.title == doc7.title
    assert meta.author == "A. Auditor"


def test_the_same_document_gives_the_same_text_twice(doc7: Document, tmp_path: Path) -> None:
    first = render_pdf(doc7, tmp_path / "a.pdf")
    second = render_pdf(doc7, tmp_path / "b.pdf")
    assert pdf_text(first) == pdf_text(second)


def test_characters_outside_winansi_are_replaced_not_dropped(tmp_path: Path) -> None:
    doc = Document(
        title="Zahlung \u20ac \u2192 \u4e2d",
        sections=(Section(1, "Cover", (Paragraph("Gr\u00fc\u00dfe \u2192 x"),)),),
        data_hash="0" * 64,
        run_date=RUN_DATE,
        tool="t",
    )
    text = pdf_text(render_pdf(doc, tmp_path / "c.pdf"))
    assert "Gr\u00fc\u00dfe ? x" in text
    assert "\u20ac" in text  # the euro sign is in WinAnsi


def test_markup_in_the_text_is_shown_as_text(tmp_path: Path) -> None:
    doc = Document(
        title="t",
        sections=(
            Section(
                1,
                "Cover",
                (
                    Paragraph("a <b>bold</b> & more"),
                    Paragraph("x <i>y</i>", "mono"),
                    Table(("<h>", "b"), (("<u>", "A & B"),)),
                ),
            ),
        ),
        data_hash="0" * 64,
        run_date=RUN_DATE,
        tool="t",
    )
    text = pdf_text(render_pdf(doc, tmp_path / "d.pdf"))
    assert "a <b>bold</b> & more" in text
    assert "x <i>y</i>" in text
    assert "<u>" in text and "A & B" in text


def test_a_minimal_document_with_all_blocks_renders(tmp_path: Path) -> None:
    doc = Document(
        title="t",
        sections=(
            Section(
                1,
                "Cover",
                (
                    Heading("sub"),
                    Paragraph("body"),
                    Paragraph("note", "note"),
                    Paragraph("a\n  b", "mono"),
                    Table(("x", "y"), (("1", "2\n3"), ("", "")), widths=(1, 2), mono_columns=(0,)),
                ),
            ),
        ),
        data_hash="0" * 64,
        run_date=RUN_DATE,
        tool="t",
    )
    text = pdf_text(render_pdf(doc, tmp_path / "m.pdf"))
    assert "sub" in text and "body" in text and "note" in text


def test_wrap_mono_wraps_long_lines_with_a_hanging_indent() -> None:
    lines = wrap_mono("  key: " + "word " * 30, 40)
    assert all(len(line) <= 40 for line in lines)
    assert lines[0].startswith("  key: word")
    assert lines[1].startswith("      word")  # continuation indented past the first line
    assert wrap_mono("short\n\nline", 40) == ["short", "", "line"]
    broken = wrap_mono("x" * 100, 40)
    assert all(len(line) <= 40 for line in broken)
    assert "".join(line.strip() for line in broken) == "x" * 100
