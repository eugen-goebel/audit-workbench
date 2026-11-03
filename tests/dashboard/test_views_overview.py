"""Overview and drill-down view models, against one seed 7 run."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path

from typer.testing import CliRunner

from audit_workbench.cli import app
from audit_workbench.core.findings import Severity
from audit_workbench.dashboard import views
from audit_workbench.reporting import (
    ModuleName,
    all_rule_ids,
    collect_evidence,
    module_of,
    sorted_findings,
)
from audit_workbench.runner import Run


def test_overview_counts_equal_awb_run(run7: Run, bank7: Path) -> None:
    result = CliRunner().invoke(app, ["run", str(bank7), "--format", "json"])
    cli_findings = json.loads(result.stdout)
    kpis = views.overview(run7).kpis
    assert kpis.findings == len(cli_findings) == 131
    assert kpis.critical == sum(f["severity"] == Severity.CRITICAL for f in cli_findings) == 7
    assert kpis.rules_fired == len({f["rule_id"] for f in cli_findings}) == 27
    assert kpis.rules_run == len(all_rule_ids(ModuleName.all)) == 27
    assert kpis.entities == len({f["entity"] for f in cli_findings})


def test_severity_by_module_sums_to_the_total(run7: Run) -> None:
    table = views.overview(run7).by_module_severity
    assert list(table.index) == ["A", "B", "C", "X"]
    assert list(table.columns) == ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    assert int(table.to_numpy().sum()) == len(run7.findings)
    assert table.sum(axis=1).to_dict() == {"A": 55, "B": 42, "C": 26, "X": 8}
    assert table.sum(axis=0).to_dict() == {"LOW": 10, "MEDIUM": 41, "HIGH": 73, "CRITICAL": 7}


def test_top_rules_list_every_rule_and_sort_by_count(run7: Run) -> None:
    top = views.overview(run7).top_rules
    counts = Counter(f.rule_id for f in run7.findings)
    assert list(top["rule"]) == sorted(top["rule"], key=lambda r: (-counts[r], r))
    assert dict(zip(top["rule"], top["count"], strict=True)) == {
        r: counts[r] for r in all_rule_ids(ModuleName.all)
    }
    assert {"rule", "title", "module", "count", "severity_mix"} <= set(top.columns)
    first = top.iloc[0]
    assert first["count"] == max(counts.values())
    assert first["title"] and first["severity_mix"]


def test_top_rules_show_a_rule_without_findings_as_zero(run7: Run) -> None:
    without_b05 = Run(
        run7.access, run7.journal, run7.change, [f for f in run7.findings if f.rule_id != "B-05"]
    )
    overview = views.overview(without_b05)
    row = overview.top_rules.set_index("rule").loc["B-05"]
    assert row["count"] == 0 and row["severity_mix"] == "-"
    assert overview.kpis.rules_fired == 26 and overview.kpis.rules_run == 27


def test_overview_for_one_module_only_counts_that_module(run7: Run) -> None:
    only_c = Run(None, None, run7.change, [f for f in run7.findings if module_of(f.rule_id) == "C"])
    overview = views.overview(only_c, ModuleName.c)
    assert overview.kpis.findings == 26
    assert overview.kpis.rules_run == 6
    assert list(overview.by_module_severity.index) == ["C"]
    assert set(overview.top_rules["module"]) == {"C"}


def test_data_basis_has_one_row_per_input_file_with_hashes(run7: Run) -> None:
    basis = views.overview(run7).data_basis
    assert len(basis) == len(collect_evidence(run7.access, run7.journal, run7.change)) == 18
    assert list(basis.columns) == ["file", "rows", "columns", "sha256", "sha256_prefix"]
    assert list(basis["file"]) == sorted(basis["file"])
    row = basis.set_index("file").loc["accounts.csv"]
    assert row["rows"] == 852 and row["columns"] == 8
    assert len(row["sha256"]) == 64 and row["sha256"].startswith(row["sha256_prefix"])
    assert len(row["sha256_prefix"]) == 12


def test_data_basis_equals_the_hashes_awb_run_prints(run7: Run, bank7: Path) -> None:
    out = CliRunner().invoke(app, ["run", str(bank7)]).stdout
    for row in views.overview(run7).data_basis.itertuples():
        assert f"{row.file}" in out and row.sha256_prefix in out


def test_evidence_quality_matches_check_ground_truth(run7: Run, truth7: dict) -> None:
    quality = views.evidence_quality(run7, truth7)
    assert quality is not None
    assert (len(quality.matched), len(quality.expected), len(quality.unexpected)) == (131, 131, 0)
    assert quality.recall == 1.0 and quality.precision == 1.0


def test_evidence_quality_is_none_without_ground_truth(run7: Run) -> None:
    assert views.evidence_quality(run7, None) is None


def test_evidence_quality_counts_findings_without_counterpart(run7: Run, truth7: dict) -> None:
    fewer = {**truth7, "issues": truth7["issues"][1:]}
    quality = views.evidence_quality(run7, fewer)
    assert quality is not None
    assert len(quality.unexpected) == 1 and len(quality.matched) == 130
    assert quality.recall == 1.0 and quality.precision == 130 / 131


def test_evidence_quality_of_one_module_ignores_the_others(run7: Run, truth7: dict) -> None:
    only_a = [f for f in run7.findings if module_of(f.rule_id) == "A"]
    quality = views.evidence_quality(Run(run7.access, None, None, only_a), truth7, ModuleName.a)
    assert quality is not None and quality.is_exact and len(quality.expected) == 55


# --- drill-down -----------------------------------------------------------------


def test_module_filter_returns_the_same_set_as_filtering_the_list(run7: Run) -> None:
    for letter in "ABCX":
        got = views.filter_findings(run7.findings, letter)
        want = [f for f in run7.findings if module_of(f.rule_id) == letter]
        assert {f.finding_id for f in got} == {f.finding_id for f in want}


def test_filters_by_severity_and_rule_match_a_plain_filter(run7: Run) -> None:
    got = views.filter_findings(
        run7.findings, "B", severities={"HIGH", "MEDIUM"}, rule_ids={"B-04", "B-08"}
    )
    want = [
        f
        for f in run7.findings
        if module_of(f.rule_id) == "B"
        and f.severity.name in {"HIGH", "MEDIUM"}
        and f.rule_id in {"B-04", "B-08"}
    ]
    assert got and {f.finding_id for f in got} == {f.finding_id for f in want}


def test_filter_result_is_sorted_most_severe_first(run7: Run) -> None:
    got = views.filter_findings(run7.findings, "A")
    assert got == sorted_findings(got)
    severities = [f.severity for f in got]
    assert severities == sorted(severities, reverse=True)


def test_empty_selection_filters_return_nothing(run7: Run) -> None:
    assert views.filter_findings(run7.findings, "A", severities=set()) == []
    assert views.filter_findings(run7.findings, "A", rule_ids=set()) == []
    assert len(views.filter_findings(run7.findings, "A", severities=None, rule_ids=None)) == 55


def test_findings_frame_columns_and_rows(run7: Run) -> None:
    frame = views.findings_frame(sorted_findings(run7.findings))
    assert list(frame.columns) == [
        "finding_id",
        "module",
        "rule",
        "title",
        "severity",
        "entity",
        "description",
    ]
    assert len(frame) == 131
    assert frame["finding_id"].is_unique
    assert views.findings_frame([]).empty


def test_findings_csv_round_trips(run7: Run) -> None:
    findings = sorted_findings(run7.findings)
    rows = list(csv.DictReader(io.StringIO(views.findings_csv(findings))))
    assert [r["finding_id"] for r in rows] == [f.finding_id for f in findings]
    assert rows[0]["severity"] in {s.name for s in Severity}


def test_findings_markdown_equals_awb_run_markdown(run7: Run, bank7: Path) -> None:
    out = CliRunner().invoke(app, ["run", str(bank7), "--format", "markdown"]).stdout
    assert views.findings_markdown(sorted_findings(run7.findings)) == out.rstrip("\n")
    assert views.findings_markdown([]) == "No findings."


def test_module_rules_list_id_and_title(run7: Run) -> None:
    rules = views.module_rules("A")
    assert [r for r, _ in rules] == [f"A-0{i}" for i in range(1, 9)]
    assert all(title for _, title in rules)
