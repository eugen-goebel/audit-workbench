"""The workpaper as a document model: sections, headings, paragraphs and tables.

The model is built once from a run (see `build.py`) and rendered twice, to DOCX and to
PDF. Both renderers read only this model, so the two formats cannot differ in content
(spec docs/design/phase-5-workpaper.md, section 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

PDF_ENCODING = "cp1252"
"""The PDF uses the built-in fonts, which cover this code page (WinAnsi) only."""

SECTION_TITLES: dict[int, str] = {
    1: "Cover",
    2: "Data basis",
    3: "Procedures",
    4: "Results",
    5: "Findings",
    6: "Samples",
    7: "Significant matters",
    8: "Sign-off",
}
"""Fixed numbers, so a reference such as "section 7" means the same in every workpaper.
Section 6 is left out of a workpaper without `--sample`."""

ISA_230_QUOTES: tuple[tuple[str, str], ...] = (
    (
        "ISA 230.8",
        "sufficient to enable an experienced auditor, having no previous connection with "
        "the audit, to understand",
    ),
    (
        "ISA 230.9(b)",
        "Who performed the audit work and the date such work was completed",
    ),
    (
        "ISA 230.9(c)",
        "Who reviewed the audit work performed and the date and extent of such review",
    ),
)
"""Verbatim extracts of `regulation/sources/isa_230.txt`; a test checks each one."""

Style = Literal["body", "note", "mono"]


@dataclass(frozen=True)
class Heading:
    """A heading below the section heading (level 2), e.g. one finding or one sample."""

    text: str
    level: int = 2


@dataclass(frozen=True)
class Paragraph:
    """Running text. `note` is smaller print, `mono` keeps the line breaks and spacing."""

    text: str
    style: Style = "body"


@dataclass(frozen=True)
class Table:
    """A table with a header row. Cells are plain text; a newline starts a new line.

    `widths` are relative column widths. `mono_columns` are the column indexes set in a
    fixed-width font (hashes, ids). Empty cells are left to the auditor on purpose.
    """

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    widths: tuple[float, ...] = ()
    mono_columns: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        width = len(self.headers)
        if any(len(row) != width for row in self.rows):
            raise ValueError("every table row needs one cell per header")
        if self.widths and len(self.widths) != width:
            raise ValueError("widths needs one value per header")


Block = Heading | Paragraph | Table


@dataclass(frozen=True)
class Section:
    number: int
    title: str
    blocks: tuple[Block, ...] = ()

    @property
    def heading(self) -> str:
        return f"{self.number}. {self.title}"


@dataclass(frozen=True)
class Document:
    """A whole workpaper: title, sections and the facts both renderers need."""

    title: str
    sections: tuple[Section, ...]
    data_hash: str
    run_date: date
    tool: str
    author: str = ""

    def section(self, number: int) -> Section:
        for section in self.sections:
            if section.number == number:
                return section
        raise KeyError(number)

    def has_section(self, number: int) -> bool:
        return any(section.number == number for section in self.sections)

    @property
    def footer_text(self) -> str:
        """The line at the foot of every page: the hash of all input files of the run."""
        return f"Data hash (SHA-256 over all input files): {self.data_hash}"

    def texts(self) -> list[str]:
        """Every text of the document in reading order: headings, paragraphs, table cells."""
        out = [self.title]
        for section in self.sections:
            out.append(section.heading)
            for block in section.blocks:
                if isinstance(block, Heading):
                    out.append(block.text)
                elif isinstance(block, Paragraph):
                    out.append(block.text)
                else:
                    out += list(block.headers)
                    for row in block.rows:
                        out += list(row)
        out.append(self.footer_text)
        return out

    def characters_outside(self, encoding: str = PDF_ENCODING) -> set[str]:
        """The characters of the document that `encoding` cannot represent."""
        found: set[str] = set()
        for text in self.texts():
            for char in text:
                try:
                    char.encode(encoding)
                except UnicodeEncodeError:
                    found.add(char)
        return found


def replace_unsupported(text: str, encoding: str = PDF_ENCODING) -> str:
    """Replace each character `encoding` cannot represent by `?`."""
    return text.encode(encoding, errors="replace").decode(encoding)
