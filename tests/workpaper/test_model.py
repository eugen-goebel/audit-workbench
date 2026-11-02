"""The document model built from a run (spec docs/design/phase-5-workpaper.md, sections 3 and 7)."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from audit_workbench import __version__
from audit_workbench.core.evidence import sha256_file
from audit_workbench.core.findings import Severity
from audit_workbench.reporting import (
    ModuleName,
    all_rule_ids,
    rule_class,
    rule_summary,
    rule_title,
    rule_version,
)
from audit_workbench.runner import Run, load_run
from audit_workbench.sampling import POPULATIONS, Sample, SampleItem, read_sample
from audit_workbench.workpaper.build import (
    DEFAULT_TITLE,
    SampleMismatchError,
    audited_entity,
    build_document,
    resolve_run_date,
    verify_sample,
)
from audit_workbench.workpaper.model import (
    ISA_230_QUOTES,
    SECTION_TITLES,
    Document,
    Heading,
    Paragraph,
    Table,
    replace_unsupported,
)

from .conftest import RUN_DATE

TOTAL_FINDINGS = 131


def _tables(doc: Document, number: int) -> list[Table]:
    return [b for b in doc.section(number).blocks if isinstance(b, Table)]


def _paragraphs(doc: Document, number: int) -> list[str]:
    return [b.text for b in doc.section(number).blocks if isinstance(b, Paragraph)]


def _findings_listed(doc: Document) -> list[str]:
    """The finding ids of section 5, in order (the second word of each heading)."""
    return [b.text.split()[0] for b in doc.section(5).blocks if isinstance(b, Heading)]


# --- structure ---------------------------------------------------------------


def test_all_eight_sections_are_present_in_order_for_seed_7(doc7: Document) -> None:
    assert [s.number for s in doc7.sections] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [s.title for s in doc7.sections] == list(SECTION_TITLES.values())
    assert [s.heading for s in doc7.sections][1] == "2. Data basis"


def test_without_a_sample_section_6_is_left_out_and_numbers_stay(bank7: Path, run7: Run) -> None:
    doc = build_document(run7, directory=bank7, module=ModuleName.all, run_date=RUN_DATE)
    assert [s.number for s in doc.sections] == [1, 2, 3, 4, 5, 7, 8]
    assert not doc.has_section(6)


def test_default_title_and_custom_title(bank7: Path, run7: Run) -> None:
    default = build_document(run7, directory=bank7, module=ModuleName.all, run_date=RUN_DATE)
    custom = build_document(
        run7, directory=bank7, module=ModuleName.all, run_date=RUN_DATE, title="Q3 review"
    )
    assert default.title == DEFAULT_TITLE == "IT audit workpaper"
    assert custom.title == "Q3 review"


# --- 1 cover -----------------------------------------------------------------


def _cover(doc: Document) -> dict[str, str]:
    table = _tables(doc, 1)[0]
    return {row[0]: row[1] for row in table.rows}


def test_cover_names_entity_dates_tool_and_run_date(doc7: Document) -> None:
    cover = _cover(doc7)
    assert cover["Audited entity"] == "synthetic bank (seed 7)"
    assert "2026-09-30" in cover["Audit date (as_of)"]
    assert "2026-02-28" in cover["Audit date (as_of)"]
    assert "postings 2025-01-01 to 2026-02-28" in cover["Periods covered"]
    assert "2025-10-01 to 2026-09-30" in cover["Periods covered"]
    assert cover["Tool"] == f"audit-workbench {__version__}"
    assert cover["Run date"] == "2026-11-02"
    assert cover["Data hash (SHA-256)"] == doc7.data_hash


def test_entity_is_the_folder_name_without_a_seed(tmp_path: Path) -> None:
    folder = tmp_path / "branch-extract"
    folder.mkdir()
    assert audited_entity(folder, None) == "branch-extract"
    assert audited_entity(folder, {"as_of": "2026-09-30"}) == "branch-extract"
    assert audited_entity(folder, {"seed": 11}) == "synthetic bank (seed 11)"


def test_cover_says_the_auditor_completes_sections_7_and_8(doc7: Document) -> None:
    text = " ".join(_paragraphs(doc7, 1))
    assert "completed by the auditor" in text
    assert "Section 7" in text and "Section 8" in text
    assert "records no conclusion" in text


def test_cover_quotes_isa_230_and_maps_it_to_sections(doc7: Document) -> None:
    text = " ".join(_paragraphs(doc7, 1))
    for ref, quote in ISA_230_QUOTES:
        assert ref in text and quote in text
    mapping = _tables(doc7, 1)[1]
    by_ref = {row[0]: row for row in mapping.rows}
    assert by_ref["8(a)"][1].startswith("3")
    assert by_ref["8(c)"][2].lower().startswith("the auditor")
    assert by_ref["9(c)"][2].lower().startswith("the reviewer")


# --- 2 data basis ------------------------------------------------------------


def test_every_input_hash_appears_in_full_in_section_2(bank7: Path, doc7: Document) -> None:
    table = _tables(doc7, 2)[0]
    assert table.headers == ("File", "Rows", "Columns", "SHA-256")
    rows = {row[0]: row for row in table.rows}
    csvs = sorted(p for p in bank7.glob("*.csv"))
    assert set(rows) == {p.name for p in csvs}  # every input file, nothing else
    for path in csvs:
        assert rows[path.name][3] == sha256_file(path)
        assert len(rows[path.name][3]) == 64


def test_section_2_rows_and_columns_match_the_files(bank7: Path, doc7: Document) -> None:
    rows = {row[0]: row for row in _tables(doc7, 2)[0].rows}
    header = (bank7 / "accounts.csv").read_text(encoding="utf-8").splitlines()[0]
    assert rows["accounts.csv"][2] == str(len(header.split(",")))
    n_rows = len((bank7 / "accounts.csv").read_text(encoding="utf-8").splitlines()) - 1
    assert rows["accounts.csv"][1] == str(n_rows)


def test_data_hash_is_the_hash_over_name_and_hash_lines(bank7: Path, doc7: Document) -> None:
    lines = "".join(
        f"{p.name} {sha256_file(p)}\n" for p in sorted(bank7.glob("*.csv"), key=lambda p: p.name)
    )
    assert doc7.data_hash == hashlib.sha256(lines.encode()).hexdigest()
    assert doc7.data_hash in " ".join(_paragraphs(doc7, 2))
    assert doc7.footer_text.endswith(doc7.data_hash)


# --- 3 procedures ------------------------------------------------------------


def test_one_procedure_row_per_rule_with_version_test_and_references(doc7: Document) -> None:
    table = _tables(doc7, 3)[0]
    ids = all_rule_ids(ModuleName.all)
    assert [row[0] for row in table.rows] == ids
    assert len(ids) == 27
    by_id = {row[0]: row for row in table.rows}
    a02 = by_id["A-02"]
    assert a02[1] == "1.0.0"
    assert rule_title("A-02") in a02[2]
    assert rule_summary("A-02") in a02[2]
    assert "dormant_days=90" in a02[3]
    assert "as_of=2026-09-30" in a02[3]
    assert "DORA Art. 9(4)(c)" in a02[4]
    assert "limit the physical or logical access" in a02[4]  # the quote
    assert "as_of=2026-02-28" in by_id["B-01"][3]  # module B runs on its own date
    assert "ruleset_path=built-in" in by_id["A-05"][3]


def test_procedures_state_timing_and_extent(doc7: Document) -> None:
    text = " ".join(_paragraphs(doc7, 3))
    assert "Timing" in text and "Extent" in text


# --- 4 results ---------------------------------------------------------------


def test_results_count_equals_the_run(run7: Run, doc7: Document) -> None:
    table = _tables(doc7, 4)[0]
    assert table.headers[:2] == ("Rule", "Title")
    sev_cols = {name: table.headers.index(name) for name in ("Low", "Medium", "High", "Critical")}
    total_col = table.headers.index("Total")
    rows = {row[0]: row for row in table.rows}
    assert set(rows) == {*all_rule_ids(ModuleName.all), "Total"}
    per_rule = Counter(f.rule_id for f in run7.findings)
    per_cell = Counter((f.rule_id, f.severity) for f in run7.findings)
    for rule_id in all_rule_ids(ModuleName.all):
        assert rows[rule_id][total_col] == str(per_rule[rule_id])
        for name, col in sev_cols.items():
            assert rows[rule_id][col] == str(per_cell[(rule_id, Severity[name.upper()])])
    assert rows["Total"][total_col] == str(len(run7.findings)) == str(TOTAL_FINDINGS)


def test_a_rule_without_findings_is_listed_as_tested_with_no_exception(
    clean_bank: Path,
) -> None:
    run = load_run(clean_bank, None, ModuleName.all)
    doc = build_document(run, directory=clean_bank, module=ModuleName.all, run_date=RUN_DATE)
    table = _tables(doc, 4)[0]
    result_col = table.headers.index("Result")
    body = [row for row in table.rows if row[0] != "Total"]
    assert len(body) == 27
    assert all(row[result_col] == "tested, no exception" for row in body)
    assert "No findings" in " ".join(_paragraphs(doc, 5))
    assert _tables(doc, 7) == []
    assert "No CRITICAL or HIGH finding" in " ".join(_paragraphs(doc, 7))


def test_filtered_findings_are_counted_in_4_but_not_listed_in_5(bank7: Path, run7: Run) -> None:
    doc = build_document(
        run7,
        directory=bank7,
        module=ModuleName.all,
        min_severity=Severity.CRITICAL,
        run_date=RUN_DATE,
    )
    critical = [f for f in run7.findings if f.severity >= Severity.CRITICAL]
    assert 0 < len(critical) < TOTAL_FINDINGS
    total_row = [r for r in _tables(doc, 4)[0].rows if r[0] == "Total"][0]
    assert total_row[_tables(doc, 4)[0].headers.index("Total")] == str(TOTAL_FINDINGS)
    note = " ".join(_paragraphs(doc, 4))
    hidden = TOTAL_FINDINGS - len(critical)
    assert f"{hidden} of {TOTAL_FINDINGS} findings" in note
    assert "not listed" in note
    assert sorted(_findings_listed(doc)) == sorted(f.finding_id for f in critical)


def test_without_a_filter_the_note_says_all_findings_are_listed(doc7: Document) -> None:
    assert "All 131 findings are listed in section 5" in " ".join(_paragraphs(doc7, 4))


def test_module_filter_limits_the_rules(bank7: Path) -> None:
    run = load_run(bank7, None, ModuleName.c)
    doc = build_document(run, directory=bank7, module=ModuleName.c, run_date=RUN_DATE)
    assert [row[0] for row in _tables(doc, 3)[0].rows] == [f"C-0{i}" for i in range(1, 7)]
    assert "Module A" not in _cover(doc)["Audit date (as_of)"]
    assert "Module C" in _cover(doc)["Audit date (as_of)"]
    assert len(_findings_listed(doc)) == 26
    files = [row[0] for row in _tables(doc, 2)[0].rows]
    assert "changes.csv" in files and "journal_lines.csv" not in files


# --- 5 findings --------------------------------------------------------------


def test_every_finding_id_of_the_run_appears_once_in_section_5(run7: Run, doc7: Document) -> None:
    listed = _findings_listed(doc7)
    assert sorted(listed) == sorted(f.finding_id for f in run7.findings)
    assert len(listed) == len(set(listed)) == TOTAL_FINDINGS


def test_findings_are_listed_most_severe_first(run7: Run, doc7: Document) -> None:
    severity = {f.finding_id: f.severity for f in run7.findings}
    levels = [severity[i] for i in _findings_listed(doc7)]
    assert levels == sorted(levels, reverse=True)
    assert levels[0] is Severity.CRITICAL


def _block(doc: Document, finding_id: str) -> tuple[Table, Paragraph]:
    blocks = doc.section(5).blocks
    for i, block in enumerate(blocks):
        if isinstance(block, Heading) and block.text.startswith(finding_id):
            table, details = blocks[i + 1], blocks[i + 2]
            assert isinstance(table, Table) and isinstance(details, Paragraph)
            return table, details
    raise AssertionError(finding_id)


def test_a_finding_block_has_rule_entity_evidence_references_and_details(
    run7: Run, doc7: Document
) -> None:
    finding = next(f for f in run7.findings if f.rule_id == "A-05")
    table, details = _block(doc7, finding.finding_id)
    fields = {row[0]: row[1] for row in table.rows}
    assert fields["Rule"] == f"A-05 v{finding.rule_version} - {rule_title('A-05')}"
    assert fields["Severity"] == finding.severity.name
    assert fields["Entity"] == finding.entity
    assert fields["Description"] == finding.description
    for sha in finding.evidence:
        assert f"{sha[:12]}" in fields["Evidence"]
        assert sha not in fields["Evidence"]  # prefix only, full hash is in section 2
    assert "accounts.csv" in fields["Evidence"]
    for ref in finding.regulation_refs:
        assert ref in fields["Regulation"]
    assert details.style == "mono"
    assert " -> " in details.text  # the access path


def test_a_journal_finding_shows_the_entry_number_and_lines(run7: Run, doc7: Document) -> None:
    finding = next(f for f in run7.findings if f.rule_id == "B-08")
    _, details = _block(doc7, finding.finding_id)
    assert finding.entity in details.text
    assert "Lines:" in details.text and "debit" in details.text


def test_benford_findings_are_shown_in_short_form(run7: Run, doc7: Document) -> None:
    finding = next(f for f in run7.findings if f.rule_id == "B-02")
    _, details = _block(doc7, finding.finding_id)
    assert "MAD" in details.text
    assert len(details.text.splitlines()) < 6  # not the 90-row digit table
    assert "awb explain" in details.text


def test_a_change_finding_shows_the_change_record(run7: Run, doc7: Document) -> None:
    finding = next(f for f in run7.findings if f.rule_id == "C-04")
    _, details = _block(doc7, finding.finding_id)
    assert "Approvals" in details.text


# --- 6 samples ---------------------------------------------------------------


def test_sample_section_documents_the_header_and_the_keys(
    doc7: Document, sample_file: Path
) -> None:
    sample = read_sample(sample_file)
    info, items = _tables(doc7, 6)
    fields = {row[0]: row[1] for row in info.rows}
    assert fields["Population"].startswith("changes")
    assert POPULATIONS["changes"].file in fields["Population"]
    assert fields["Population hash (SHA-256)"] == sample.sha256
    assert fields["Population size (N)"] == str(sample.population_size)
    assert fields["Sample size (n)"] == str(sample.n)
    assert fields["Method"] == "binomial"
    assert fields["Confidence"] == "95%"
    assert fields["Tolerable deviation rate"] == "5%"
    assert fields["Expected deviation rate"] == "1%"
    assert fields["Seed"] == "3"
    assert items.headers == ("Rank", "Key", "Role", "Result")
    assert [r[1] for r in items.rows] == [i.key for i in sample.items]
    assert {r[2] for r in items.rows} == {"sample", "replacement"}
    assert all(r[3] == "" for r in items.rows)  # the auditor records the result


def test_several_samples_become_several_parts(bank7: Path, run7: Run, sample_file: Path) -> None:
    sample = read_sample(sample_file)
    doc = build_document(
        run7,
        directory=bank7,
        module=ModuleName.all,
        samples=[sample, sample],
        run_date=RUN_DATE,
    )
    assert len(_tables(doc, 6)) == 4
    assert sum(isinstance(b, Heading) for b in doc.section(6).blocks) == 2


def test_a_sample_of_other_data_is_rejected(bank7: Path, sample_file: Path) -> None:
    sample = read_sample(sample_file)
    verify_sample(sample, bank7)  # the matching sample passes
    other = Sample(
        population=sample.population,
        file=sample.file,
        sha256="0" * 64,
        population_size=sample.population_size,
        seed=sample.seed,
        replacements=0,
        parameters=sample.parameters,
        items=(SampleItem(1, "CHG-00001"),),
    )
    with pytest.raises(SampleMismatchError, match="different data"):
        verify_sample(other, bank7)


def test_an_unknown_population_is_rejected(bank7: Path) -> None:
    sample = Sample("nope", "nope", "0" * 64, 1, 1, 0, {}, (SampleItem(1, "x"),))
    with pytest.raises(ValueError, match="unknown population"):
        verify_sample(sample, bank7)


# --- 7 significant matters, 8 sign-off ---------------------------------------


def test_section_7_has_one_open_row_per_critical_and_high_finding(
    run7: Run, doc7: Document
) -> None:
    table = _tables(doc7, 7)[0]
    assert table.headers == (
        "Finding",
        "Rule, severity, entity",
        "Discussed with / when",
        "Conclusion",
        "Judgment",
    )
    expected = sorted(
        f.finding_id for f in run7.findings if f.severity in (Severity.CRITICAL, Severity.HIGH)
    )
    assert sorted(row[0] for row in table.rows) == expected
    assert len(expected) > 0
    assert all(row[2] == row[3] == row[4] == "" for row in table.rows)
    assert "completed by the auditor" in " ".join(_paragraphs(doc7, 7))


def test_signoff_names_the_preparer_and_leaves_the_reviewer_empty(doc7: Document) -> None:
    fields = {row[0]: row[1] for row in _tables(doc7, 8)[0].rows}
    assert fields["Prepared by"] == "A. Auditor"
    assert fields["Date of the run (work completed)"] == "2026-11-02"
    assert fields["Reviewed by"] == ""
    assert fields["Date of review"] == ""
    assert fields["Extent of review"] == ""


def test_preparer_defaults_to_not_recorded(bank7: Path, run7: Run) -> None:
    doc = build_document(run7, directory=bank7, module=ModuleName.all, run_date=RUN_DATE)
    fields = {row[0]: row[1] for row in _tables(doc, 8)[0].rows}
    assert fields["Prepared by"] == "not recorded"
    assert doc.author == ""


# --- run date, characters ----------------------------------------------------


def test_source_date_epoch_fixes_the_run_date() -> None:
    epoch = int(datetime(2026, 5, 17, 23, 59, tzinfo=UTC).timestamp())
    assert resolve_run_date({"SOURCE_DATE_EPOCH": str(epoch)}) == date(2026, 5, 17)
    assert resolve_run_date({}) == date.today()
    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        resolve_run_date({"SOURCE_DATE_EPOCH": "yesterday"})


def test_the_document_text_stays_inside_the_pdf_character_set(doc7: Document) -> None:
    """The built-in PDF fonts cover WinAnsi (cp1252). Report what falls outside, if anything."""
    outside = doc7.characters_outside()
    assert outside == set(), f"replaced in the PDF: {sorted(outside)}"


def test_unsupported_characters_are_replaced_and_found() -> None:
    doc = Document(
        title="Zahlung \u20ac \u2192 \u4e2d",
        sections=(),
        data_hash="0" * 64,
        run_date=RUN_DATE,
        tool="t",
    )
    assert doc.characters_outside() == {"\u2192", "\u4e2d"}  # the euro sign is in WinAnsi
    assert replace_unsupported(doc.title) == "Zahlung \u20ac ? ?"


# --- small contracts ---------------------------------------------------------


def test_a_table_needs_one_cell_and_one_width_per_header() -> None:
    with pytest.raises(ValueError, match="one cell per header"):
        Table(("a", "b"), (("1",),))
    with pytest.raises(ValueError, match="one value per header"):
        Table(("a", "b"), (("1", "2"),), widths=(1,))


def test_a_missing_section_is_a_key_error(doc7: Document) -> None:
    with pytest.raises(KeyError):
        doc7.section(9)


def test_an_unknown_rule_has_no_class_summary_or_version() -> None:
    assert rule_class("Z-99") is None
    assert rule_summary("Z-99") == ""
    assert rule_version("Z-99") == "?"
    assert rule_version("A-01") == "1.0.0"


def test_a_missing_population_file_is_a_mismatch(tmp_path: Path, sample_file: Path) -> None:
    with pytest.raises(SampleMismatchError, match="not found"):
        verify_sample(read_sample(sample_file), tmp_path)


def test_a_run_without_exceptions_above_the_filter_says_so(bank7: Path) -> None:
    run = load_run(bank7, None, ModuleName.a)
    only_high = Run(
        run.access, run.journal, run.change, [f for f in run.findings if f.severity < 3]
    )
    doc = build_document(
        only_high,
        directory=bank7,
        module=ModuleName.a,
        min_severity=Severity.CRITICAL,
        run_date=RUN_DATE,
    )
    assert "No finding reaches the minimum severity" in " ".join(_paragraphs(doc, 5))
    assert "not listed" in " ".join(_paragraphs(doc, 4))
