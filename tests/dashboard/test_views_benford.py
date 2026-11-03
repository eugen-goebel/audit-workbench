"""Benford view: groups, sample size guard, links to B-01 and B-02."""

from __future__ import annotations

import inspect

import pytest

from audit_workbench.dashboard import views
from audit_workbench.journal.benford import THRESHOLDS
from audit_workbench.journal.rules import BenfordFirstDigit, BenfordFirstTwo
from audit_workbench.runner import Run


def test_group_counts_on_seed_7(run7: Run) -> None:
    assert len(views.benford_view(run7.journal, "employee").groups) == 33
    assert len(views.benford_view(run7.journal, "account").groups) == 72


def test_minimum_sizes_are_the_defaults_of_the_rules() -> None:
    def default(cls: type) -> int:
        return inspect.signature(cls.__init__).parameters["min_n"].default

    assert views.MIN_N == {
        "employee": default(BenfordFirstDigit),
        "account": default(BenfordFirstTwo),
    }
    assert views.MIN_N == {"employee": 1000, "account": 5000}


@pytest.mark.parametrize("by", ["employee", "account"])
def test_small_groups_are_not_testable_not_clean(run7: Run, by: str) -> None:
    view = views.benford_view(run7.journal, by)
    small = [g for g in view.groups if g.n < view.min_n]
    big = [g for g in view.groups if g.n >= view.min_n]
    assert small and big
    for group in small:
        assert not group.testable and group.status == "not testable"
        assert not group.nonconforming
    for group in big:
        assert group.testable and group.status in ("conforming", "nonconforming")


@pytest.mark.parametrize(("by", "rule"), [("employee", "B-01"), ("account", "B-02")])
def test_nonconforming_testable_groups_are_exactly_the_findings(
    run7: Run, by: str, rule: str
) -> None:
    view = views.benford_view(run7.journal, by, findings=run7.findings)
    findings = {f.entity: f for f in run7.findings if f.rule_id == rule}
    flagged = {g.key for g in view.groups if g.testable and g.nonconforming}
    assert flagged == set(findings) and flagged
    for group in view.groups:
        assert group.finding_id == (
            findings[group.key].finding_id if group.key in findings else None
        )


def test_threshold_and_digit_table(run7: Run) -> None:
    first = views.benford_view(run7.journal, "employee")
    assert first.digits == 1 and first.threshold == THRESHOLDS[1][2] == 0.015
    assert len(views.benford_table(first.groups[0])) == 9
    two = views.benford_view(run7.journal, "account")
    assert two.digits == 2 and two.threshold == THRESHOLDS[2][2] == 0.0022
    table = views.benford_table(two.groups[0])
    assert len(table) == 90 and list(table.columns) == [
        "digit",
        "expected",
        "observed",
        "count",
        "z",
    ]
    assert table["count"].sum() == two.groups[0].n


def test_digits_override_changes_the_table_but_not_the_finding_link(run7: Run) -> None:
    view = views.benford_view(run7.journal, "employee", digits=2, findings=run7.findings)
    assert view.digits == 2 and len(views.benford_table(view.groups[0])) == 90
    assert view.threshold == THRESHOLDS[2][2]
    assert all(g.finding_id is None for g in view.groups)


def test_groups_are_sorted_by_key(run7: Run) -> None:
    keys = [g.key for g in views.benford_view(run7.journal, "account").groups]
    assert keys == sorted(keys)


def test_invalid_arguments(run7: Run) -> None:
    with pytest.raises(ValueError, match="by"):
        views.benford_view(run7.journal, "weekday")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="digits"):
        views.benford_view(run7.journal, "employee", digits=3)
