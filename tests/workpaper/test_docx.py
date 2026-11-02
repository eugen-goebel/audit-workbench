"""The DOCX renderer, checked by opening the file with python-docx (spec section 7)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from docx import Document as OpenDocx

from audit_workbench.runner import Run
from audit_workbench.workpaper.model import Document, Heading, Paragraph, Section, Table
from audit_workbench.workpaper.render_docx import render_docx

from .conftest import RUN_DATE
from .extract import docx_text, docx_texts, squash


@pytest.fixture(scope="module")
def docx_path(doc7: Document, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return render_docx(doc7, tmp_path_factory.mktemp("docx") / "workpaper.docx")


@pytest.fixture(scope="module")
def opened(docx_path: Path):
    return OpenDocx(str(docx_path))


def test_the_headings_of_all_eight_sections_are_headings(doc7: Document, opened) -> None:
    level1 = [p.text for p in opened.paragraphs if p.style.name == "Heading 1"]
    assert level1 == [s.heading for s in doc7.sections]
    assert level1[0] == "1. Cover" and level1[-1] == "8. Sign-off"


def test_the_title_is_the_document_title(doc7: Document, opened) -> None:
    titles = [p.text for p in opened.paragraphs if p.style.name == "Title"]
    assert titles == [doc7.title]


def test_every_finding_id_is_in_the_document(run7: Run, docx_path: Path) -> None:
    text = docx_text(docx_path)
    missing = [f.finding_id for f in run7.findings if f.finding_id not in text]
    assert not missing


def test_the_footer_carries_the_data_hash(doc7: Document, opened) -> None:
    footer = " ".join(p.text for p in opened.sections[0].footer.paragraphs)
    assert doc7.data_hash in footer
    assert "Page" in footer


def test_the_tables_are_real_word_tables(doc7: Document, opened) -> None:
    model_tables = [b for s in doc7.sections for b in s.blocks if isinstance(b, Table)]
    assert len(opened.tables) == len(model_tables)
    for model, word in zip(model_tables, opened.tables, strict=True):
        assert tuple(c.text for c in word.rows[0].cells) == model.headers
        assert len(word.rows) == len(model.rows) + 1


def test_every_text_of_the_model_is_in_the_document(doc7: Document, docx_path: Path) -> None:
    present = {squash(t) for t in docx_texts(docx_path)}
    absent = [t for t in doc7.texts()[:-1] if squash(t) and squash(t) not in present]
    assert not absent[:5], absent[:5]


def test_section_7_conclusions_and_reviewer_fields_are_empty(doc7: Document, opened) -> None:
    by_header = {t.rows[0].cells[0].text: t for t in opened.tables}
    significant = by_header["Finding"]
    assert [c.text for c in significant.rows[0].cells][2:] == [
        "Discussed with / when",
        "Conclusion",
        "Judgment",
    ]
    for row in significant.rows[1:]:
        assert [c.text for c in row.cells][2:] == ["", "", ""]
    sign_off = {
        row.cells[0].text: row.cells[1].text
        for t in opened.tables
        for row in t.rows
        if row.cells[0].text in ("Reviewed by", "Date of review", "Extent of review")
    }
    assert sign_off == {"Reviewed by": "", "Date of review": "", "Extent of review": ""}


def test_page_is_a4_and_properties_are_fixed_by_the_run_date(doc7: Document, opened) -> None:
    section = opened.sections[0]
    assert round(section.page_width.cm, 1) == 21.0
    assert round(section.page_height.cm, 1) == 29.7
    props = opened.core_properties
    assert props.title == doc7.title
    assert props.author == "A. Auditor"
    assert props.created == datetime(2026, 11, 2, tzinfo=UTC)
    assert props.modified == props.created


def test_the_same_document_gives_the_same_text_twice(doc7: Document, tmp_path: Path) -> None:
    first = render_docx(doc7, tmp_path / "a.docx")
    second = render_docx(doc7, tmp_path / "b.docx")
    assert docx_text(first) == docx_text(second)


def test_a_minimal_document_renders(tmp_path: Path) -> None:
    doc = Document(
        title="t",
        sections=(
            Section(
                1,
                "Cover",
                (
                    Heading("h"),
                    Paragraph("body"),
                    Paragraph("note", "note"),
                    Paragraph("a\n  b", "mono"),
                    Table(("x", "y"), (("1", "2\n3"),), widths=(1, 2), mono_columns=(0,)),
                ),
            ),
        ),
        data_hash="0" * 64,
        run_date=RUN_DATE,
        tool="t",
    )
    path = render_docx(doc, tmp_path / "m.docx")
    assert "2\n3" in docx_text(path)
    assert "a\n  b" in docx_text(path)
