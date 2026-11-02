"""Tests for the regulation mapping (spec docs/design/phase-4-crosslink.md, section 5).

The mapping is only worth something if its quotes are real. These tests therefore
check the quotes against the source files in `regulation/sources/`, check that
every registered rule has an entry, and check that the reference a finding carries
is always one of the mapped ones.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.access.rules import ACCESS_RULES, run_access_rules
from audit_workbench.change.data import load_change_data
from audit_workbench.change.rules import CHANGE_RULES
from audit_workbench.crosslink import CROSSLINK_RULES, run_crosslink_rules
from audit_workbench.journal.data import load_journal_data
from audit_workbench.journal.rules import JOURNAL_RULES
from audit_workbench.regulation import (
    MAPPING_ENV,
    MAPPING_PATH,
    MappingError,
    Reference,
    apply_references,
    load_mapping,
)
from audit_workbench.regulation.mapping import reference_strings
from audit_workbench.synth import generate_bank

SOURCES = MAPPING_PATH.parent / "sources"
AS_OF = date(2026, 9, 30)
ALL_RULE_IDS = {
    *ACCESS_RULES.ids(),
    *JOURNAL_RULES.ids(),
    *CHANGE_RULES.ids(),
    *CROSSLINK_RULES.ids(),
}


def _normalise(text: str) -> str:
    """Collapse whitespace and typographic quotes, so a wrapped quote still matches."""
    text = text.replace("\u00a0", " ").replace("\u2019", "'").replace("\u201c", '"')
    text = text.replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip()


def _source_by_url() -> dict[str, str]:
    """Map each `Source:` URL in the source files to the normalised text of that file."""
    by_url: dict[str, str] = {}
    for path in SOURCES.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"^Source:\s*(\S+)\s*$", text, re.MULTILINE)
        if match:
            by_url[match.group(1)] = _normalise(text)
    return by_url


def _entries() -> Iterator[tuple[str, Reference]]:
    for rule_id, refs in load_mapping().items():
        for ref in refs:
            yield rule_id, ref


# --- structure ---------------------------------------------------------------


def test_mapping_file_exists() -> None:
    assert MAPPING_PATH.is_file(), f"{MAPPING_PATH} is missing"


def test_every_registered_rule_has_an_entry() -> None:
    """Spec section 5: no rule without a provision, or an explicit empty list."""
    mapping = load_mapping()
    assert ALL_RULE_IDS <= set(mapping), f"no entry for {sorted(ALL_RULE_IDS - set(mapping))}"


def test_mapping_names_only_registered_rules() -> None:
    """A typo in a rule id would silently never reach a finding, so it fails here."""
    unknown = set(load_mapping()) - ALL_RULE_IDS
    assert not unknown, f"mapping names rules that do not exist: {sorted(unknown)}"


def test_entries_have_all_four_fields() -> None:
    for rule_id, ref in _entries():
        assert ref.framework, f"{rule_id}: framework"
        assert ref.ref, f"{rule_id}: ref"
        assert ref.quote, f"{rule_id}: quote"
        assert ref.url.startswith("https://"), f"{rule_id}: url {ref.url!r}"


def test_access_and_crosslink_rules_cite_the_umbrella_provision() -> None:
    """Every access finding rests on DORA Art. 9(4)(c); the specific article comes on top."""
    mapping = load_mapping()
    for rule_id in (*ACCESS_RULES.ids(), "X-02"):
        assert any(str(ref).startswith("DORA Art. 9(4)(c)") for ref in mapping[rule_id]), rule_id
    for rule_id in ACCESS_RULES.ids():
        assert len(mapping[rule_id]) >= 2, f"{rule_id} needs a specific provision too"


# --- quotes ------------------------------------------------------------------


def test_every_quote_is_a_substring_of_its_source_file() -> None:
    """The point of the mapping: a quote that cannot be found in the source is a citation bug."""
    by_url = _source_by_url()
    problems: list[str] = []
    for rule_id, ref in _entries():
        text = by_url.get(ref.url)
        if text is None:
            problems.append(f"{rule_id}: no source file for {ref.url}")
        elif _normalise(ref.quote) not in text:
            problems.append(f"{rule_id} {ref.ref}: quote not found in its source")
    assert not problems, "\n".join(problems)


def test_quotes_are_long_enough_to_be_meaningful() -> None:
    """A three-word "quote" would pass the substring test without supporting anything."""
    short = [(rule_id, ref.ref) for rule_id, ref in _entries() if len(ref.quote.split()) < 8]
    assert not short, f"quotes shorter than eight words: {short}"


def test_urls_point_at_the_official_text() -> None:
    """ELI links for the EU acts, the publisher for ISA 240 and GoBD."""
    allowed = (
        "https://data.europa.eu/eli/",  # DORA and the RTS, ELI
        "https://www.iaasb.org/",  # ISA 240 (Revised)
        "https://amtliche-handbuecher.bundesfinanzministerium.de/",  # GoBD
    )
    for rule_id, ref in _entries():
        assert ref.url.startswith(allowed), f"{rule_id}: {ref.url}"


# --- findings ----------------------------------------------------------------


def _bank(tmp_path: Path) -> Path:
    generate_bank(tmp_path, seed=2)
    return tmp_path


def test_finding_references_are_all_mapped(tmp_path: Path) -> None:
    """A reference on a finding must come from the mapping, so the two cannot drift apart."""
    directory = _bank(tmp_path)
    access = AccessData(evidence=load_access_evidence(directory), as_of=AS_OF)
    journal = load_journal_data(directory, as_of=AS_OF)
    change = load_change_data(directory, as_of=AS_OF)
    findings = [*run_access_rules(access), *run_crosslink_rules(access, journal, change)]
    known = reference_strings()
    stray = {(f.rule_id, ref) for f in findings for ref in f.regulation_refs if ref not in known}
    assert not stray, f"references not in the mapping: {sorted(stray)}"


def test_findings_gain_the_mapped_reference(tmp_path: Path) -> None:
    """The access rules set no reference themselves, so the mapping fills them in."""
    access = AccessData(evidence=load_access_evidence(_bank(tmp_path)), as_of=AS_OF)
    findings = run_access_rules(access)
    assert findings
    for finding in findings:
        assert finding.regulation_refs, f"{finding.rule_id} has no reference"
        assert all("Art. " in ref for ref in finding.regulation_refs), finding.regulation_refs


def test_apply_references_keeps_the_rules_own_wording() -> None:
    """A rule that cites its provision itself keeps that text; the mapping adds the rest."""
    from audit_workbench.core.findings import Finding, Severity

    own = "ISA 240 (Revised) para. 49(d): entries throughout the period"
    finding = Finding(
        rule_id="B-01",
        rule_version="1",
        severity=Severity.MEDIUM,
        entity="JE-1",
        description="test",
        evidence=["a" * 64],
        regulation_refs=[own],
    )
    mapping = {
        "B-01": (
            Reference("ISA 240 (Revised)", "para. 49(d): entries throughout the period", "x", "u"),
            Reference("ISA 240 (Revised)", "para. 48: appropriateness", "x", "u"),
        )
    }
    [result] = apply_references([finding], mapping)
    assert result.regulation_refs[0] == own
    assert "ISA 240 (Revised) para. 48: appropriateness" in result.regulation_refs
    assert len(result.regulation_refs) == 2


def test_apply_references_leaves_complete_findings_untouched() -> None:
    from audit_workbench.core.findings import Finding, Severity

    finding = Finding(
        rule_id="Z-99",
        rule_version="1",
        severity=Severity.LOW,
        entity="X",
        description="test",
        evidence=["b" * 64],
    )
    assert apply_references([finding], {})[0] is finding


# --- graceful degradation ----------------------------------------------------


def test_missing_mapping_file_gives_no_references(tmp_path: Path) -> None:
    """An installed wheel has no repository root; the run must still work."""
    assert load_mapping(tmp_path / "nope.yaml") == {}


def test_malformed_mapping_file_raises(tmp_path: Path) -> None:
    """A broken mapping would hide a broken citation, so it fails loudly."""
    broken = tmp_path / "mapping.yaml"
    broken.write_text("A-01:\n  - framework: DORA\n    ref: Art. 1\n", encoding="utf-8")
    with pytest.raises(MappingError, match="lacks"):
        load_mapping(broken)


def test_environment_variable_overrides_the_path(tmp_path: Path, monkeypatch) -> None:
    """A deployment can point the workbench at its own copy of the mapping."""
    own = tmp_path / "own.yaml"
    own.write_text(
        "A-01:\n"
        "  - framework: DORA\n"
        "    ref: 'Art. 9(4)(c): test'\n"
        "    quote: a quote long enough to be meaningful\n"
        "    url: https://data.europa.eu/eli/reg/2022/2554/oj\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(MAPPING_ENV, str(own))
    assert "A-01" in load_mapping()
    monkeypatch.delenv(MAPPING_ENV)
    assert "A-01" in load_mapping()  # back to the repository mapping


def test_mapping_is_cached(tmp_path: Path) -> None:
    """The file is parsed once per path, not once per finding."""
    path = tmp_path / "mapping.yaml"
    path.write_text("A-01: []\n", encoding="utf-8")
    first = load_mapping(path)
    path.write_text("A-02: []\n", encoding="utf-8")
    assert load_mapping(path) is first, "the second read must come from the cache"
    assert "A-02" not in first
