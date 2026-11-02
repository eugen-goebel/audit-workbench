"""Every ISA 230 quote the workpaper prints is a substring of the stored source text."""

from __future__ import annotations

import re
from pathlib import Path

from audit_workbench.regulation import MAPPING_PATH
from audit_workbench.workpaper.model import ISA_230_QUOTES

SOURCE = MAPPING_PATH.parent / "sources" / "isa_230.txt"


def _normalise(text: str) -> str:
    """Collapse whitespace and typographic quotes, so a wrapped quote still matches."""
    text = text.replace("\u00a0", " ").replace("\u2019", "'").replace("\u201c", '"')
    text = text.replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip()


def test_the_source_file_exists() -> None:
    assert SOURCE.is_file()


def test_every_isa_230_quote_is_a_substring_of_its_source_file() -> None:
    source = _normalise(Path(SOURCE).read_text(encoding="utf-8"))
    assert ISA_230_QUOTES, "the workpaper quotes nothing"
    for ref, quote in ISA_230_QUOTES:
        assert _normalise(quote) in source, f"{ref}: not found in {SOURCE.name}: {quote!r}"


def test_the_quotes_cover_paragraphs_8_and_9() -> None:
    refs = {ref for ref, _ in ISA_230_QUOTES}
    assert any(r.startswith("ISA 230.8") for r in refs)
    assert any(r.startswith("ISA 230.9") for r in refs)
