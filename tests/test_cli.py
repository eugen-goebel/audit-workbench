"""Tests for the `awb` command line, run end to end against the synthetic bank."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from audit_workbench.cli import app
from audit_workbench.core.evidence import sha256_file

runner = CliRunner()
# 55 access issues (module A) + 42 journal issues (module B) + 26 change issues (module C)
ISSUES_A, ISSUES_B, ISSUES_C = 55, 42, 26
TOTAL_ISSUES = ISSUES_A + ISSUES_B + ISSUES_C  # 123
# Phase 4: ground_truth.json also lists the 8 cross-module issues (X-01 to X-03). The CLI does
# not know module X yet, so the tests below compare the A, B and C part and strip the X rows
# before `check-ground-truth` (remove together with these helpers once cli.py runs module X).
ISSUES_X = 8


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
    _strip_x(target)
    return target


def _truth(directory: Path) -> dict:
    return json.loads((directory / "ground_truth.json").read_text())


def _strip_x(directory: Path) -> None:
    """Drop the module X rows from ground_truth.json (the CLI has no module X yet)."""
    truth = _truth(directory)
    truth["issues"] = [i for i in truth["issues"] if not i["rule_id"].startswith("X-")]
    (directory / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n")


def _rows(directory: Path, name: str) -> list[dict[str, str]]:
    with (directory / f"{name}.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


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
    assert f"{TOTAL_ISSUES + ISSUES_X} injected issues" in lines[0]
    assert " employees" in lines[0] and " accounts" in lines[0]
    assert " changes" in lines[0] and " deployments" in lines[0]
    assert f"(A {ISSUES_A}, B {ISSUES_B}, C {ISSUES_C})" in lines[0]
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
    assert "(A 0, B 0, C 0)" in result.stdout
    assert _truth(out)["issues"] == []


# --- run ---------------------------------------------------------------------


def test_run_table_lists_every_finding_with_summary_and_hashes(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "2026-09-30" in out.splitlines()[0]  # audit date is stated up front
    header = next(line for line in out.splitlines() if line.startswith("finding_id"))
    for column in ("module", "rule", "severity", "entity", "description"):
        assert column in header
    findings = _run_json(bank)
    assert len(findings) == TOTAL_ISSUES == len(_truth(bank)["issues"]) - ISSUES_X
    for finding in findings:
        assert finding["finding_id"] in out
    # the change date is stated next to the others
    assert "Change as_of: 2026-09-30" in out
    # per-rule count summary, with the rules of all three modules
    assert "A-05" in out and "B-10" in out and "C-06" in out and "Total" in out
    # traceability: first 12 hex chars of every input file's SHA-256
    for csv_file in sorted(bank.glob("*.csv")):
        assert csv_file.name in out
        assert sha256_file(csv_file)[:12] in out


def test_run_table_is_sorted_by_severity_then_rule_then_entity(bank: Path) -> None:
    findings = _run_json(bank)
    keys = [(f["rule_id"][0], -f["severity"], f["rule_id"], f["entity"]) for f in findings]
    assert keys == sorted(keys)
    assert [k[0] for k in keys][:1] == ["A"] and [k[0] for k in keys][-1:] == ["C"]
    assert {k[0] for k in keys} == {"A", "B", "C"}
    result = runner.invoke(app, ["run", str(bank)])
    positions = [result.stdout.index(f["finding_id"]) for f in findings]
    assert positions == sorted(positions)


def test_run_json_matches_ground_truth(bank: Path) -> None:
    findings = _run_json(bank)
    got = {(f["rule_id"], f["entity"]) for f in findings}
    want = {(i["rule_id"], i["entity"]) for i in _truth(bank)["issues"] if i["rule_id"][0] != "X"}
    assert got == want
    assert all(len(f["finding_id"]) == 16 for f in findings)


def test_run_markdown_is_a_github_table(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank), "--format", "markdown"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines[0].startswith("| finding_id | module | rule | severity | entity | description |")
    assert set(lines[1]) <= set("|- ")
    assert all(line.startswith("|") and line.endswith("|") for line in lines)
    assert len(lines) == TOTAL_ISSUES + 2
    assert sum(" | C | C-" in line for line in lines) == ISSUES_C


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
    expected = _run_json(bank_copy, "--module", "a")
    (bank_copy / "ground_truth.json").unlink()
    assert _run_json(bank_copy, "--module", "a", "--as-of", "2026-09-30") == expected


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


def test_check_ground_truth_passes_on_generated_bank(bank_copy: Path) -> None:
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.startswith("## ")
    assert "seed" in out.lower() and "2026-09-30" in out
    assert "- As of module C: 2026-09-30" in out
    assert (
        "| module | rule | title | expected | found | true positives | recall | precision |" in out
    )
    assert "| A | A-05 | Segregation-of-duties conflict | 9 | 9 | 9 | 100.0% | 100.0% |" in out
    assert "| A | **Total A** |  | 55 | 55 | 55 | 100.0% | 100.0% |" in out
    assert "| B | **Total B** |  | 42 | 42 | 42 | 100.0% | 100.0% |" in out
    assert "| C | C-01 | Production deployment without an approved change | 6 | 6 | 6 |" in out
    assert "| C | C-02 | Developer deployed their own change | 5 | 5 | 5 | 100.0% | 100.0% |" in out
    assert "| C | **Total C** |  | 26 | 26 | 26 | 100.0% | 100.0% |" in out
    rows = [line for line in out.splitlines() if line.startswith("| C | C-")]
    assert [r.split(" | ")[1] for r in rows] == [f"C-0{n}" for n in range(1, 7)]
    assert (
        out.index("| A | **Total A**")
        < out.index("| B | **Total B**")
        < out.index("| C | **Total C**")
    )
    assert (
        f"|  | **Total** |  | {TOTAL_ISSUES} | {TOTAL_ISSUES} | {TOTAL_ISSUES} | 100.0% | 100.0% |"
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
    precision = f"{100 * (TOTAL_ISSUES - 1) / TOTAL_ISSUES:.1f}%"  # 122 of 123 findings
    assert precision == "99.2%"
    assert (
        f"|  | **Total** |  | {TOTAL_ISSUES - 1} | {TOTAL_ISSUES} | {TOTAL_ISSUES - 1} | "
        f"100.0% | {precision} |" in result.stdout
    )


def test_check_ground_truth_fails_when_an_expected_issue_is_not_found(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    truth["issues"].append({"rule_id": "A-03", "entity": "GHOST-1", "note": "invented"})
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 1
    assert "Missed" in result.stdout
    assert "`A-03` `GHOST-1`" in result.stdout
    assert "| A | A-03 | " in result.stdout and "| 4 | 3 | 3 | 75.0% | 100.0% |" in result.stdout


def test_check_ground_truth_on_clean_bank_shows_na(tmp_path: Path) -> None:
    out = tmp_path / "clean"
    assert runner.invoke(app, ["generate", str(out), "--clean"]).exit_code == 0
    result = runner.invoke(app, ["check-ground-truth", str(out)])
    assert result.exit_code == 0, result.output
    assert "| A | A-01 | " in result.stdout
    assert "| 0 | 0 | 0 | n/a | n/a |" in result.stdout


def test_check_ground_truth_reports_a_missed_change_issue(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    truth["issues"].append({"rule_id": "C-03", "entity": "CHG-99999", "note": "invented"})
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 1
    assert "`C-03` `CHG-99999`" in result.stdout
    assert "| C | C-03 | " in result.stdout and "| 5 | 4 | 4 | 80.0% | 100.0% |" in result.stdout
    assert "| C | **Total C** |  | 27 | 26 | 26 | 96.3% | 100.0% |" in result.stdout
    assert "| A | **Total A** |  | 55 | 55 | 55 | 100.0% | 100.0% |" in result.stdout


def test_check_ground_truth_does_not_file_unknown_rules_under_module_b(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    truth["issues"].append({"rule_id": "Z-01", "entity": "X", "note": "invented"})
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 1
    assert "| ? | Z-01 | (unknown rule) | 1 | 0 | 0 | 0.0% | n/a |" in result.stdout
    assert "| B | **Total B** |  | 42 | 42 | 42 | 100.0% | 100.0% |" in result.stdout


def test_check_ground_truth_requires_ground_truth_file(bank_copy: Path) -> None:
    (bank_copy / "ground_truth.json").unlink()
    result = runner.invoke(app, ["check-ground-truth", str(bank_copy)])
    assert result.exit_code == 2
    assert "ground_truth.json" in result.output


# --- module B and new options ------------------------------------------------


def test_run_module_b_only(bank: Path) -> None:
    findings = _run_json(bank, "--module", "b")
    assert len(findings) == 42
    assert all(f["rule_id"].startswith("B-") for f in findings)


def test_run_module_a_only(bank: Path) -> None:
    findings = _run_json(bank, "--module", "a")
    assert len(findings) == 55
    assert all(f["rule_id"].startswith("A-") for f in findings)


def test_run_rank_appends_selection_aid(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank), "--rank", "5"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Anomaly ranking" in out and "selection aid" in out
    ranking = out.split("Anomaly ranking", 1)[1]
    assert "rank" in ranking and "score" in ranking and "doc_no" in ranking
    assert "MJ-" in ranking


def test_run_rank_rejected_with_json(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank), "--rank", "5", "--format", "json"])
    assert result.exit_code == 2
    assert "--rank" in result.output


def test_benford_by_employee_marks_small_groups(bank: Path) -> None:
    result = runner.invoke(app, ["benford", str(bank), "--by", "employee"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "MAD" in out and "chi-square" in out
    assert "too small to judge" in out
    assert "digit" in out and "expected" in out and "observed" in out


def test_benford_rejects_unknown_grouping(bank: Path) -> None:
    result = runner.invoke(app, ["benford", str(bank), "--by", "month"])
    assert result.exit_code == 2


def test_explain_b10_shows_entry_and_lines(bank: Path) -> None:
    finding = _first(_run_json(bank), "B-10")
    result = runner.invoke(app, ["explain", str(bank), finding["finding_id"]])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert finding["entity"] in out
    assert "Lines:" in out
    assert "preparer:" in out and "approver:" in out


def test_explain_b01_shows_digit_table(bank: Path) -> None:
    finding = _first(_run_json(bank), "B-01")
    result = runner.invoke(app, ["explain", str(bank), finding["finding_id"]])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "digit" in out and "expected" in out and "observed" in out
    assert "MAD" in out and "assessment:" in out


# --- module C ----------------------------------------------------------------


def test_run_module_c_only(bank: Path) -> None:
    findings = _run_json(bank, "--module", "c")
    assert len(findings) == ISSUES_C
    assert all(f["rule_id"].startswith("C-") for f in findings)
    want = {(i["rule_id"], i["entity"]) for i in _truth(bank)["issues"] if i["rule_id"][0] == "C"}
    assert {(f["rule_id"], f["entity"]) for f in findings} == want


def test_run_module_c_table_only_shows_change_rules(bank: Path) -> None:
    result = runner.invoke(app, ["run", str(bank), "--module", "c"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.splitlines()[0] == "Change as_of: 2026-09-30"
    assert "Audit date" not in out and "Journal as_of" not in out
    assert "C-06" in out and "A-05" not in out and "B-10" not in out
    assert "Total" in out
    for name in ("changes", "change_approvals", "commits", "deployments"):
        assert f"{name}.csv" in out
    assert "ledger" not in out
    markdown = runner.invoke(app, ["run", str(bank), "--module", "c", "--format", "markdown"])
    assert len([ln for ln in markdown.stdout.splitlines() if ln.strip()]) == ISSUES_C + 2


def test_run_change_as_of_is_read_from_ground_truth(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    truth["change_as_of"] = "2026-06-30"
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    result = runner.invoke(app, ["run", str(bank_copy), "--module", "c"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "Change as_of: 2026-06-30"
    assert len(_run_json(bank_copy, "--module", "c")) != ISSUES_C


def test_run_change_as_of_falls_back_to_bank_as_of(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    del truth["change_as_of"]
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    result = runner.invoke(app, ["run", str(bank_copy), "--module", "c"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "Change as_of: 2026-09-30"


def test_run_module_c_needs_as_of_without_ground_truth(bank_copy: Path) -> None:
    (bank_copy / "ground_truth.json").unlink()
    result = runner.invoke(app, ["run", str(bank_copy), "--module", "c"])
    assert result.exit_code == 2
    assert "--as-of is required for module C" in result.output
    explicit = _run_json(bank_copy, "--module", "c", "--as-of", "2026-09-30")
    assert len(explicit) == ISSUES_C


def test_run_module_c_missing_change_file_exits_2(bank_copy: Path) -> None:
    (bank_copy / "change_approvals.csv").unlink()
    result = runner.invoke(app, ["run", str(bank_copy), "--module", "c"])
    assert result.exit_code == 2
    assert "change_approvals" in result.output


def test_run_module_a_does_not_need_change_files(bank_copy: Path) -> None:
    (bank_copy / "deployments.csv").unlink()
    assert len(_run_json(bank_copy, "--module", "a")) == ISSUES_A
    assert len(_run_json(bank_copy, "--module", "b")) == ISSUES_B


def _explain(directory: Path, finding: dict) -> str:
    result = runner.invoke(app, ["explain", str(directory), finding["finding_id"]])
    assert result.exit_code == 0, result.output
    return result.stdout


def _c_finding(bank: Path, rule_id: str, **details: object) -> dict:
    return next(
        f
        for f in _run_json(bank, "--module", "c")
        if f["rule_id"] == rule_id and all(f["details"].get(k) == v for k, v in details.items())
    )


def test_explain_c01_missing_change_shows_the_deployment_itself(bank: Path) -> None:
    finding = _c_finding(bank, "C-01", triggers=["missing"])
    out = _explain(bank, finding)
    dep = next(d for d in _rows(bank, "deployments") if d["deployment_id"] == finding["entity"])
    assert dep["change_id"] == ""
    employee = finding["details"]["deploying_employee_id"]
    name = next(e["name"] for e in _rows(bank, "hr_employees") if e["employee_id"] == employee)
    assert "C-01 v1.0.0 - Production deployment without an approved change" in out
    assert f"Flagged deployment: {finding['entity']}" in out
    assert "Change: none (the deployment carries no change id)" in out
    assert "Approvals" not in out and "Linked commits" not in out
    production = out.split("Production deployments:")[1]
    for value in (
        finding["entity"],
        dep["deployed_ts"],
        dep["method"],
        dep["account_id"],
        f"{employee} ({name})",
        dep["result"],
    ):
        assert value in production
    assert 'triggers: ["missing"]' in out
    assert "change_id: null" in out


def test_explain_c01_unknown_change_says_it_is_not_in_the_change_file(bank: Path) -> None:
    finding = _c_finding(bank, "C-01", triggers=["unknown"])
    out = _explain(bank, finding)
    assert f"Change: none (change {finding['details']['change_id']} is not in changes.csv)" in out
    assert f"Flagged deployment: {finding['entity']}" in out
    assert "Production deployments:" in out and finding["entity"] in out.split("Production")[1]


def test_explain_c01_cancelled_or_unapproved_change_shows_the_change(bank: Path) -> None:
    finding = _c_finding(bank, "C-01", triggers=["not_approved"])
    out = _explain(bank, finding)
    change_id = finding["details"]["change_id"]
    assert f"change_id:     {change_id}" in out
    assert f"Flagged deployment: {finding['entity']}" in out
    assert "Approvals (time order):" in out


def test_explain_c02_shows_change_approvals_commits_and_deployment(bank: Path) -> None:
    finding = _c_finding(bank, "C-02", method="manual", via_second_account=False)
    out = _explain(bank, finding)
    people = {e["employee_id"]: e["name"] for e in _rows(bank, "hr_employees")}
    dep = next(d for d in _rows(bank, "deployments") if d["deployment_id"] == finding["entity"])
    change = next(c for c in _rows(bank, "changes") if c["change_id"] == dep["change_id"])
    employee = finding["details"]["deploying_employee_id"]
    lines = out.splitlines()

    assert "C-02 v1.0.0 - Developer deployed their own change" in out
    assert f"Flagged deployment: {finding['entity']}" in lines[lines.index("Details:") + 1]
    # the change
    assert f"    change_id:     {change['change_id']}" in lines
    assert f"    system:        {change['system']}" in lines
    assert f"    type:          {change['change_type']}" in lines
    assert f"    risk:          {change['risk']}" in lines
    requester = change["requester_employee_id"]
    assert f"    requester:     {requester} ({people[requester]})" in lines
    assert f"    status:        {change['status']}" in lines
    tester = change["tester_employee_id"]
    signoff = f"{change['test_signoff_ts']} by {tester} ({people[tester]})"
    assert f"    test sign-off: {signoff}" in lines
    # approvals, in time order
    approvals = sorted(
        (a for a in _rows(bank, "change_approvals") if a["change_id"] == change["change_id"]),
        key=lambda a: a["decided_ts"],
    )
    section = out.split("Approvals (time order):")[1].split("Linked commits:")[0]
    assert section.index("stage") < section.index("approver") < section.index("decided")
    positions = []
    for a in approvals:
        who = f"{a['approver_employee_id']} ({people[a['approver_employee_id']]})"
        line = next(ln for ln in section.splitlines() if a["decided_ts"] in ln)
        assert a["approval_stage"] in line and who in line and a["decision"] in line
        positions.append(section.index(a["decided_ts"]))
    assert positions == sorted(positions)
    # linked commits with authors
    commits = [c for c in _rows(bank, "commits") if c["change_id"] == change["change_id"]]
    section = out.split("Linked commits:")[1].split("Production deployments:")[0]
    assert len(commits) >= 2
    for c in commits:
        line = next(ln for ln in section.splitlines() if c["commit_sha"] in ln)
        assert f"{c['author_employee_id']} ({people[c['author_employee_id']]})" in line
        assert c["committed_ts"] in line
    # the production deployment
    section = out.split("Production deployments:")[1].split("Rule details:")[0]
    line = next(ln for ln in section.splitlines() if finding["entity"] in ln)
    for value in (
        dep["deployed_ts"],
        "manual",
        dep["account_id"],
        f"{employee} ({people[employee]})",
        dep["result"],
    ):
        assert value in line
    assert f'commit_shas: ["{finding["details"]["commit_shas"][0]}"' in out
    assert "via_second_account: false" in out


def test_explain_c02_second_account_and_pipeline_release(bank: Path) -> None:
    second = _c_finding(bank, "C-02", via_second_account=True)
    assert "via_second_account: true" in _explain(bank, second)
    pipeline = _c_finding(bank, "C-02", method="pipeline")
    out = _explain(bank, pipeline)
    employee = pipeline["details"]["deploying_employee_id"]
    line = next(ln for ln in out.split("Production deployments:")[1].splitlines() if "DEP-" in ln)
    assert "pipeline" in line and pipeline["details"]["account_id"] in line and employee in line


def test_explain_c04_lists_the_late_approval_before_the_deployment(bank: Path) -> None:
    finding = _c_finding(bank, "C-04")
    out = _explain(bank, finding)
    details = finding["details"]
    assert "C-04 v1.0.0 - Approval came after the deployment" in out
    assert "Flagged deployment" not in out  # reported per change, not per deployment
    assert f"    change_id:     {finding['entity']}" in out
    approvals = out.split("Approvals (time order):")[1].split("Linked commits:")[0]
    assert details["approved_ts"] in approvals and "approved" in approvals
    deployments = out.split("Production deployments:")[1]
    assert details["first_deployment_id"] in deployments
    assert details["deployed_ts"] in deployments
    assert details["deployed_ts"] < details["approved_ts"]
    assert f"hours_before_approval: {json.dumps(details['hours_before_approval'])}" in out


def test_explain_c06_without_test_sign_off_says_none(bank: Path) -> None:
    finding = _c_finding(bank, "C-06", trigger="missing")
    out = _explain(bank, finding)
    assert "    test sign-off: none" in out


def test_explain_c03_missing_post_approval_and_late_post_approval(bank: Path) -> None:
    missing = [
        f
        for f in _run_json(bank, "--module", "c")
        if f["details"].get("trigger") == "missing" and f["rule_id"] == "C-03"
    ]
    assert len(missing) == 2
    decided = {a["change_id"] for a in _rows(bank, "change_approvals")}
    for finding in missing:
        out = _explain(bank, finding)
        assert "    type:          emergency" in out
        assert 'trigger: "missing"' in out and "post_days: 7" in out
        assert (finding["entity"] in decided) == ("Approvals (time order):\n    none" not in out)
    # every production attempt of the change is listed, not only the first
    prod = [d for d in _rows(bank, "deployments") if d["environment"] == "prod"]
    for finding in missing:
        out = _explain(bank, finding)
        for dep in (d for d in prod if d["change_id"] == finding["entity"]):
            assert dep["deployment_id"] in out
    late = _c_finding(bank, "C-03", days_after_deployment=8)
    out = _explain(bank, late)
    assert 'trigger: "late"' in out
    approvals = out.split("Approvals (time order):")[1].split("Linked commits:")[0]
    assert "post" in approvals and late["details"]["approved_ts"] in approvals


def test_explain_c05_shows_the_approver_who_built_the_change(bank: Path) -> None:
    finding = _c_finding(bank, "C-05")
    out = _explain(bank, finding)
    approver = finding["details"]["approver_employee_id"]
    assert approver in out.split("Approvals (time order):")[1].split("Linked commits:")[0]
    assert "Linked commits:" in out and "Production deployments:" in out


def test_explain_c_change_without_commits_and_with_unknown_requester(bank_copy: Path) -> None:
    commits = bank_copy / "commits.csv"
    commits.write_text(commits.read_text().splitlines()[0] + "\n")
    finding = _c_finding(bank_copy, "C-04")
    changes = bank_copy / "changes.csv"
    rows = _rows(bank_copy, "changes")
    next(r for r in rows if r["change_id"] == finding["entity"])["requester_employee_id"] = "E-GONE"
    with changes.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    out = _explain(bank_copy, _c_finding(bank_copy, "C-04"))
    assert "Linked commits:\n    none" in out
    assert "    requester:     -" in out.splitlines()


def test_explain_states_a_change_date_that_differs_from_the_audit_date(bank_copy: Path) -> None:
    truth = _truth(bank_copy)
    truth["change_as_of"] = "2026-06-30"
    (bank_copy / "ground_truth.json").write_text(json.dumps(truth))
    finding = _c_finding(bank_copy, "C-02")
    assert "module C 2026-06-30" in _explain(bank_copy, finding)
