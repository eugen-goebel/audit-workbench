"""Finding detail: rule text, references with quotes, evidence hashes, raw rows."""

from __future__ import annotations

import pytest

from audit_workbench.dashboard import views
from audit_workbench.regulation.mapping import load_mapping
from audit_workbench.reporting import (
    collect_evidence,
    render_details,
    rule_summary,
    rule_title,
    rule_version,
)
from audit_workbench.runner import Run


def _first(run: Run, rule_id: str):
    return next(f for f in run.findings if f.rule_id == rule_id)


def test_detail_carries_the_rule_text_and_parameters(run7: Run) -> None:
    finding = _first(run7, "A-01")
    detail = views.finding_detail(run7, finding)
    assert detail.finding is finding
    assert detail.rule_title == rule_title("A-01")
    assert detail.rule_summary == rule_summary("A-01") and detail.rule_summary
    assert detail.rule_version == rule_version("A-01")
    assert detail.params == finding.params
    assert detail.detail_lines == render_details(finding, run7.access, run7.journal, run7.change)


def test_detail_lists_evidence_files_with_full_hash_and_prefix(run7: Run) -> None:
    finding = _first(run7, "A-01")
    detail = views.finding_detail(run7, finding)
    by_hash = {ev.sha256: name for name, ev in collect_evidence(run7.access).items()}
    assert [e.sha256 for e in detail.evidence] == finding.evidence
    assert [e.file for e in detail.evidence] == [f"{by_hash[h]}.csv" for h in finding.evidence]
    assert all(e.sha256.startswith(e.prefix) and len(e.prefix) == 12 for e in detail.evidence)


def test_references_carry_their_quote_from_the_mapping(run7: Run) -> None:
    finding = _first(run7, "A-05")
    detail = views.finding_detail(run7, finding)
    assert [r.label for r in detail.references] == finding.regulation_refs
    mapped = {str(ref): ref for ref in load_mapping()["A-05"]}
    quoted = [r for r in detail.references if r.label in mapped]
    assert quoted
    for ref in quoted:
        assert ref.quote == mapped[ref.label].quote and ref.url == mapped[ref.label].url


def test_every_finding_has_a_detail_and_quotes_where_the_mapping_has_them(run7: Run) -> None:
    for finding in run7.findings:
        detail = views.finding_detail(run7, finding)
        known = {str(r) for r in load_mapping().get(finding.rule_id, ())}
        for ref in detail.references:
            assert (ref.quote is not None) == (ref.label in known)
            assert ref.url is None or ref.url.startswith("https://")
        assert detail.rule_title and detail.evidence


def test_raw_rows_for_an_account_finding_are_the_account_row(run7: Run) -> None:
    finding = _first(run7, "A-01")
    detail = views.finding_detail(run7, finding)
    accounts = next(r for r in detail.raw_rows if r.file == "accounts.csv")
    assert accounts.total == 1 and list(accounts.rows["account_id"]) == [finding.entity]
    assert "account_id" in accounts.key


def test_raw_rows_for_a_journal_finding_include_entry_and_lines(run7: Run) -> None:
    finding = _first(run7, "B-04")
    detail = views.finding_detail(run7, finding)
    files = {r.file: r for r in detail.raw_rows}
    assert list(files["journal_entries.csv"].rows["doc_no"]) == [finding.entity]
    assert files["journal_lines.csv"].total >= 2
    assert set(files["journal_lines.csv"].rows["doc_no"]) == {finding.entity}


def test_raw_rows_for_a_change_finding_cover_the_change_chain(run7: Run) -> None:
    finding = _first(run7, "C-03")
    files = {r.file: r for r in views.finding_detail(run7, finding).raw_rows}
    assert list(files["changes.csv"].rows["change_id"]) == [finding.entity]
    assert set(files["deployments.csv"].rows["change_id"]) == {finding.entity}


def test_raw_rows_for_a_deployment_finding_use_the_deployment_id(run7: Run) -> None:
    finding = _first(run7, "C-01")
    files = {r.file: r for r in views.finding_detail(run7, finding).raw_rows}
    assert list(files["deployments.csv"].rows["deployment_id"]) == [finding.entity]


def test_raw_rows_for_x01_follow_the_journal_entries_it_names(run7: Run) -> None:
    finding = _first(run7, "X-01")
    files = {r.file: r for r in views.finding_detail(run7, finding).raw_rows}
    assert set(files["journal_entries.csv"].rows["doc_no"]) == set(finding.details["doc_nos"])
    employee = finding.entity.split(":")[0]
    assert set(files["accounts.csv"].rows["employee_id"]) == {employee}


def test_raw_rows_are_capped_and_say_how_many_there_are(run7: Run) -> None:
    finding = _first(run7, "B-02")
    files = {r.file: r for r in views.finding_detail(run7, finding, max_rows=20).raw_rows}
    lines = files["journal_lines.csv"]
    assert lines.total > 20 and len(lines.rows) == 20 and lines.truncated
    assert set(lines.rows["account_no"]) == {finding.entity}


def test_population_level_findings_have_no_raw_rows(run7: Run) -> None:
    assert views.finding_detail(run7, _first(run7, "B-01")).raw_rows == []


@pytest.mark.parametrize("rule_id", ["A-04", "A-06", "A-08", "B-06", "B-07", "X-02", "X-03"])
def test_raw_rows_exist_for_entity_keyed_rules(run7: Run, rule_id: str) -> None:
    detail = views.finding_detail(run7, _first(run7, rule_id))
    assert detail.raw_rows and all(r.total > 0 for r in detail.raw_rows)
