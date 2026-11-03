"""The ground-truth comparison lives in reporting.py, so the CLI and the dashboard share it."""

from __future__ import annotations

from audit_workbench.core.findings import Finding, Severity
from audit_workbench.reporting import (
    GroundTruthComparison,
    ModuleName,
    compare_ground_truth,
    ground_truth_report,
)

HASH = "a" * 64


def _finding(rule_id: str, entity: str) -> Finding:
    return Finding(
        rule_id=rule_id,
        rule_version="1.0.0",
        severity=Severity.HIGH,
        entity=entity,
        description="x",
        evidence=[HASH],
    )


def _truth(*pairs: tuple[str, str]) -> dict:
    return {
        "seed": 7,
        "as_of": "2026-09-30",
        "issues": [{"rule_id": r, "entity": e} for r, e in pairs],
    }


def test_exact_match_has_full_recall_and_precision() -> None:
    result = compare_ground_truth(
        _truth(("A-01", "e1"), ("B-03", "e2")), [_finding("A-01", "e1"), _finding("B-03", "e2")]
    )
    assert isinstance(result, GroundTruthComparison)
    assert result.is_exact
    assert (result.recall, result.precision) == (1.0, 1.0)
    assert len(result.matched) == 2 and not result.missed and not result.unexpected


def test_missed_and_unexpected_are_separated() -> None:
    result = compare_ground_truth(
        _truth(("A-01", "e1"), ("A-01", "e2")), [_finding("A-01", "e1"), _finding("A-02", "e3")]
    )
    assert not result.is_exact
    assert result.matched == {("A-01", "e1")}
    assert result.missed == {("A-01", "e2")}
    assert result.unexpected == {("A-02", "e3")}
    assert result.recall == 0.5 and result.precision == 0.5


def test_empty_sides_give_none_not_a_division_error() -> None:
    result = compare_ground_truth(_truth(), [])
    assert result.is_exact
    assert result.recall is None and result.precision is None


def test_module_restricts_the_expected_issues() -> None:
    truth = _truth(("A-01", "e1"), ("B-03", "e2"))
    result = compare_ground_truth(truth, [_finding("A-01", "e1")], ModuleName.a)
    assert result.is_exact and result.expected == {("A-01", "e1")}


def test_report_is_the_markdown_the_cli_prints() -> None:
    result = compare_ground_truth(_truth(("A-01", "e1")), [_finding("A-01", "e1")])
    text = ground_truth_report(result)
    assert text.startswith("## Ground-truth check\n\n- Seed: 7\n")
    assert "- Result: **PASS**" in text
    assert "| A | **Total A** |  | 1 | 1 | 1 | 100.0% | 100.0% |" in text
    assert "### Missed" not in text and "### Unexpected" not in text


def test_report_lists_missed_and_unexpected_with_fail_verdict() -> None:
    result = compare_ground_truth(_truth(("A-01", "e1")), [_finding("A-01", "e9")])
    text = ground_truth_report(result)
    assert "- Result: **FAIL**" in text
    assert "### Missed (expected, not found)\n\n- `A-01` `e1`" in text
    assert "### Unexpected (found, not expected)\n\n- `A-01` `e9`" in text
