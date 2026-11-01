"""Tests for the `awb` command line, run end to end against the synthetic bank."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from audit_workbench.cli import app
from audit_workbench.core.evidence import sha256_file

runner = CliRunner()
TOTAL_ISSUES = 55  # sum of the injected counts in spec section 5


@pytest.fixture(scope="module")
def bank(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One injected bank (seed 7), shared by read-only tests."""
    out = tmp_path_factory.mktemp("cli") / "bank"
    result = runner.invoke(app, ["generate", str(out)])
    assert result.exit_code == 0, result.output
    return out


@pytest.fixture
def bank_copy(bank: Path, tmp_path: Path) -> Path:
    """A private copy of the bank for tests that edit files."""
    target = tmp_path / "copy"
    shutil.copytree(bank, target)
    return target


def _truth(directory: Path) -> dict:
    return json.loads((directory / "ground_truth.json").read_text())


def _run_json(directory: Path, *extra: str) -> list[dict]:
    result = runner.invoke(app, ["run", str(directory), "--format", "json", *extra])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# --- generate ----------------------------------------------------------------


def test_generate_prints_one_summary_line(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    result = runner.invoke(app, ["generate", str(out)])
    assert result.exit_code == 0, result.output
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert str(out) in lines[0]
    assert f"{TOTAL_ISSUES} injected issues" in lines[0]
    assert " employees" in lines[0] and " accounts" in lines[0]
    assert (out / "ground_truth.json").is_file()


def test_generate_seed_option_is_recorded(tmp_path: Path) -> None:
    out = tmp_path / "s11"
    result = runner.invoke(app, ["generate", str(out), "--seed", "11"])
    assert result.exit_code == 0, result.output
    assert _truth(out)["seed"] == 11


def test_generate_clean_has_no_issues(tmp_path: Path) -> None:
    out = tmp_path / "clean"
    result = runner.invoke(app, ["generate", str(out), "--clean"])
    assert result.exit_code == 0, result.output
    assert "0 injected issues" in result.stdout
    assert _truth(out)["issues"] == []


# --- run ---------------------------------------------------------------------


def test_run_table_lists_every_finding_with_summary_and_hashes(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "2026-09-30" in out.splitlines()[0]  # audit date is stated up front
    header = next(line for line in out.splitlines() if line.startswith("finding_id"))
    for column in ("rule", "severity", "entity", "description"):
        assert column in header
    findings = _run_json(bank)
    assert len(findings) == TOTAL_ISSUES
    for finding in findings:
        assert finding["finding_id"] in out
    # per-rule count summary
    assert "A-05" in out and "Total" in out
    # traceability: first 12 hex chars of every input file's SHA-256
    for csv_file in sorted(bank.glob("*.csv")):
        assert csv_file.name in out
        assert sha256_file(csv_file)[:12] in out


def test_run_table_is_sorted_by_severity_then_rule_then_entity(bank: Path) -> None:
    findings = _run_json(bank)
    keys = [(-f["severity"], f["rule_id"], f["entity"]) for f in findings]
    assert keys == sorted(keys)
    result = runner.invoke(app, ["run", str(bank)])
    positions = [result.stdout.index(f["finding_id"]) for f in findings]
    assert positions == sorted(positions)


def test_run_json_matches_ground_truth(bank: Path) -> None:
    findings = _run_json(bank)
    got = {(f["rule_id"], f["entity"]) for f in findings}
    want = {(i["rule_id"], i["entity"]) for i in _truth(bank)["issues"]}
    assert got == want
    assert all(len(f["finding_id"]) == 16 for f in findings)


def test_run_markdown_is_a_github_table(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank), "--format", "markdown"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines[0].startswith("| finding_id | rule | severity | entity | description |")
    assert set(lines[1]) <= set("|- ")
    assert all(line.startswith("|") and line.endswith("|") for line in lines)
    assert len(lines) == TOTAL_ISSUES + 2


def test_run_output_option_writes_file_instead_of_stdout(bank: Path, tmp_path: Path) -> None:
    target = tmp_path / "findings.json"
    result = runner.invoke(app, ["run", str(bank), "--format", "json", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert len(json.loads(target.read_text())) == TOTAL_ISSUES
    assert "finding_id" not in result.stdout


def test_run_min_severity_filters(bank: Path) -> None:
    everything = _run_json(bank)
    high = _run_json(bank, "--min-severity", "high")
    assert 0 < len(high) < len(everything)
    assert all(f["severity"] >= 3 for f in high)
    assert {f["finding_id"] for f in high} == {
        f["finding_id"] for f in everything if f["severity"] >= 3
    }
    assert any(f["severity"] == 1 for f in everything)
    assert _run_json(bank, "--min-severity", "low") == everything


def test_run_min_severity_rejects_unknown_value(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank), "--min-severity", "urgent"])
    assert result.exit_code == 2


def test_run_without_as_of_and_ground_truth_exits_2(bank_copy: Path) -> None:
    (bank_copy / "ground_truth.json").unlink()
    result = runner.invoke(app, ["run", str(bank_copy)])
    assert result.exit_code == 2
    assert "--as-of is required" in result.output


def test_run_explicit_as_of_works_without_ground_truth(bank_copy: Path) -> None:
    expected = _run_json(bank_copy)
    (bank_copy / "ground_truth.json").unlink()
    assert _run_json(bank_copy, "--as-of", "2026-09-30") == expected


def test_run_as_of_changes_the_result(bank: Path) -> None:
    later = _run_json(bank, "--as-of", "2027-09-30")
    assert {f["finding_id"] for f in later} != {f["finding_id"] for f in _run_json(bank)}


def test_run_rejects_malformed_as_of(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank), "--as-of", "30.09.2026"])
    assert result.exit_code == 2


def test_run_clean_bank_has_no_findings(tmp_path: Path) -> None:
    out = tmp_path / "clean"
    assert runner.invoke(app, ["generate", str(out), "--clean"]).exit_code == 0
    assert _run_json(out) == []
    table = runner.invoke(app, ["run", str(out)])
    assert table.exit_code == 0
    assert "No findings" in table.stdout
    markdown = runner.invoke(app, ["run", str(out), "--format", "markdown"])
    assert markdown.exit_code == 0
    assert "No findings" in markdown.stdout


def test_run_missing_directory_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", str(tmp_path / "nope"), "--as-of", "2026-09-30"])
    assert result.exit_code == 2


def test_run_missing_input_file_exits_2(bank_copy: Path) -> None:
    (bank_copy / "accounts.csv").unlink()
    result = runner.invoke(app, ["run", str(bank_copy)])
    assert result.exit_code == 2
    assert "accounts" in result.output


# --- explain -----------------------------------------------------------------


def _first(findings: list[dict], rule_id: str) -> dict:
    return next(f for f in findings if f["rule_id"] == rule_id)


def test_explain_a05_renders_access_paths(bank: Path) -> None:
    finding = _first(_run_json(bank), "A-05")
    result = runner.invoke(app, ["explain", str(bank), finding["finding_id"]])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert finding["finding_id"] in out
    assert "Segregation-of-duties conflict" in out  # rule title
    assert finding["entity"] in out
    assert finding["description"] in out
    for function, path in finding["details"].items():
        assert f"{function}: {' -> '.join(path)}" in out
    assert " -> " in out
    # evidence hashes are listed in full, with the evidence name
    for digest in finding["evidence"]:
        assert digest in out
    assert "accounts" in out
    assert "as_of" in out  # params


def test_explain_other_rule_prints_details(bank: Path) -> None:
    finding = _first(_run_json(bank), "A-01")
    result = runner.invoke(app, ["explain", str(bank), finding["finding_id"]])
    assert result.exit_code == 0, result.output
    assert "days_since_exit" in result.stdout
    assert "A-01 v1.0.0 - Account active after employee exit" in result.stdout


def test_explain_accepts_unique_prefix(bank: Path) -> None:
    ids = [f["finding_id"] for f in _run_json(bank)]
    target = ids[0]
    prefix = next(
        target[:n] for n in range(1, 17) if sum(i.startswith(target[:n]) for i in ids) == 1
    )
    result = runner.invoke(app, ["explain", str(bank), prefix])
    assert result.exit_code == 0, result.output
    assert target in result.stdout


def test_explain_ambiguous_prefix_exits_1(bank: Path) -> None:
    ids = [f["finding_id"] for f in _run_json(bank)]
    shared = next(c for c in "0123456789abcdef" if sum(i.startswith(c) for i in ids) > 1)
    result = runner.invoke(app, ["explain", str(bank), shared])
    assert result.exit_code == 1
    assert "ambiguous" in result.output.lower()


def test_explain_unknown_id_exits_1(bank: Path) -> None:
    result = runner.invoke(app, ["explain", str(bank), "zzzz"])
    assert result.exit_code == 1
    assert "zzzz" in result.output


# --- check-ground-truth ------------------------------------------------------


def test_check_ground_truth_passes_on_generated_bank(bank: Path) -> None:
    result = runner.invoke(app, ["check-ground-truth", str(bank)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.startswith("## ")
    assert "seed" in out.lower() and "2026-09-30" in out
    assert "| rule | title | expected | found | true positives | recall | precision |" in out
    assert "| A-05 | Segregation-of-duties conflict | 9 | 9 | 9 | 100.0% | 100.0% |" in out
    assert (
        f"| **Total** |  | {TOTAL_ISSUES} | {TOTAL_ISSUES} | {TOTAL_ISSUES} | 100.0% | 100.0% |"
        in out
    )
    assert "Missed" not in out and "Unexpected" not in out


def test_check_ground_truth_fails_when_a_finding_is_not_in_ground_truth(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    removed = truth["issues"].pop(0)
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 1
    assert "Unexpected" in result.stdout
    assert f"`{removed['rule_id']}` `{removed['entity']}`" in result.stdout
    assert "Missed" not in result.stdout
    assert f"| {TOTAL_ISSUES - 1} | {TOTAL_ISSUES} | {TOTAL_ISSUES - 1} | 100.0% | 98.2% |" in (
        result.stdout
    )


def test_check_ground_truth_fails_when_an_expected_issue_is_not_found(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    truth["issues"].append({"rule_id": "A-03", "entity": "GHOST-1", "note": "invented"})
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 1
    assert "Missed" in result.stdout
    assert "`A-03` `GHOST-1`" in result.stdout
    assert "| A-03 | " in result.stdout and "| 4 | 3 | 3 | 75.0% | 100.0% |" in result.stdout


def test_check_ground_truth_on_clean_bank_shows_na(tmp_path: Path) -> None:
    out = tmp_path / "clean"
    assert runner.invoke(app, ["generate", str(out), "--clean"]).exit_code == 0
    result = runner.invoke(app, ["check-ground-truth", str(out)])
    assert result.exit_code == 0, result.output
    assert "| A-01 | " in result.stdout
    assert "| 0 | 0 | 0 | n/a | n/a |" in result.stdout


def test_check_ground_truth_requires_ground_truth_file(bank_copy: Path) -> None:
    (bank_copy / "ground_truth.json").unlink()
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 2
    assert "ground_truth.json" in result.output
