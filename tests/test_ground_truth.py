"""Ground-truth acceptance tests for the synthetic bank (spec section 5)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.access.rules import run_access_rules
from audit_workbench.synth import generate_bank

AS_OF = date(2026, 9, 30)
SPEC_COUNTS = {
    "A-01": 12,
    "A-02": 10,
    "A-03": 3,
    "A-04": 6,
    "A-05": 9,
    "A-06": 3,
    "A-07": 4,
    "A-08": 8,
}


def _want(truth: dict) -> set[tuple[str, str]]:
    """Module A keys: module B issues are appended to the same ground truth."""
    return {
        (issue["rule_id"], issue["entity"])
        for issue in truth["issues"]
        if issue["rule_id"].startswith("A-")
    }


def _run(directory: Path) -> tuple[list, dict]:
    data = AccessData(evidence=load_access_evidence(Path(directory)), as_of=AS_OF)
    findings = run_access_rules(data)
    truth = json.loads((Path(directory) / "ground_truth.json").read_text())
    return findings, truth


@pytest.fixture(scope="module")
def bank(tmp_path_factory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("gt"), seed=7)


def test_findings_equal_ground_truth_exactly(bank: Path) -> None:
    findings, truth = _run(bank)
    got = {(f.rule_id, f.entity) for f in findings}
    want = _want(truth)
    assert got == want, (
        f"differs from ground truth: missing={sorted(want - got)} extra={sorted(got - want)}"
    )


def test_counts_match_spec_table(bank: Path) -> None:
    _, truth = _run(bank)
    counts = Counter(i["rule_id"] for i in truth["issues"] if i["rule_id"].startswith("A-"))
    assert dict(counts) == SPEC_COUNTS
    assert truth["seed"] == 7 and truth["as_of"] == AS_OF.isoformat()


def test_no_findings_without_injection(tmp_path: Path) -> None:
    directory = generate_bank(tmp_path / "clean", seed=7, inject=False)
    findings, truth = _run(directory)
    assert findings == [] and truth["issues"] == []


def test_second_seed_also_exact(tmp_path: Path) -> None:
    directory = generate_bank(tmp_path / "seed11", seed=11)
    findings, truth = _run(directory)
    got = {(f.rule_id, f.entity) for f in findings}
    assert got == _want(truth)
    assert dict(Counter(f.rule_id for f in findings)) == SPEC_COUNTS


@pytest.mark.parametrize("seed", [2, 19])
def test_fragile_seeds_generate_and_match_full_ground_truth(tmp_path: Path, seed: int) -> None:
    """Regressions found by a sweep over seeds 1 to 30 (all modules, via the CLI).

    Seed 2: the cross-account GL employee joined in March 2025, was not
    employed for the whole fiscal year, and the B-08 injection raised.
    Seed 19: a B-07 original was posted back into March but entered in April,
    so its clone was entered first and B-07 reported the original instead.
    """
    from typer.testing import CliRunner  # noqa: PLC0415

    from audit_workbench.cli import app  # noqa: PLC0415

    directory = generate_bank(tmp_path / f"seed{seed}", seed=seed)
    result = CliRunner().invoke(app, ["check-ground-truth", str(directory)])
    assert result.exit_code == 0, result.output
