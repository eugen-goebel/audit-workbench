"""Tests for the deterministic synthetic bank generator."""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import networkx as nx
import pytest

from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.access.rules import run_access_rules
from audit_workbench.access.sod import employee_functions
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


def a_issues(truth: dict) -> set[tuple[str, str]]:
    """Ground-truth keys of module A (module B issues live in the same file)."""
    return {
        (issue["rule_id"], issue["entity"])
        for issue in truth["issues"]
        if issue["rule_id"].startswith("A-")
    }


def load(directory: Path) -> AccessData:
    return AccessData(evidence=load_access_evidence(Path(directory)), as_of=AS_OF)


@pytest.fixture(scope="module")
def injected_dir(tmp_path_factory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("injected"), seed=7)


@pytest.fixture(scope="module")
def clean_dir(tmp_path_factory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("clean"), seed=7, inject=False)


def test_injected_bank_matches_ground_truth(injected_dir: Path) -> None:
    findings = run_access_rules(load(injected_dir))
    truth = json.loads((injected_dir / "ground_truth.json").read_text())
    got = {(f.rule_id, f.entity) for f in findings}
    want = a_issues(truth)
    missing = sorted(want - got)
    extra = sorted(got - want)
    assert not missing and not extra, f"missing={missing} extra={extra}"
    assert dict(Counter(f.rule_id for f in findings)) == SPEC_COUNTS
    assert truth["as_of"] == AS_OF.isoformat() and truth["seed"] == 7


def test_clean_bank_has_no_findings(clean_dir: Path) -> None:
    assert run_access_rules(load(clean_dir)) == []


def test_ground_truth_empty_without_injection(clean_dir: Path) -> None:
    truth = json.loads((clean_dir / "ground_truth.json").read_text())
    assert truth["issues"] == []


def test_ground_truth_issues_sorted(injected_dir: Path) -> None:
    truth = json.loads((injected_dir / "ground_truth.json").read_text())
    keys = [(i["rule_id"], i["entity"]) for i in truth["issues"]]
    assert keys == sorted(keys)
    assert all(i["note"] for i in truth["issues"])


def test_files_written(clean_dir: Path) -> None:
    expected = (
        {f"{name}.csv" for name in load_access_evidence(clean_dir)}
        | {
            "gl_accounts.csv",
            "journal_entries.csv",
            "journal_lines.csv",
            "close_calendar.csv",
            "approval_limits.csv",
        }
        | {"changes.csv", "change_approvals.csv", "commits.csv", "deployments.csv"}
        | {"ground_truth.json"}
    )
    assert {p.name for p in clean_dir.iterdir()} == expected


@pytest.mark.parametrize("seed", [7, 11])
def test_determinism_same_seed(tmp_path: Path, seed: int) -> None:
    a = generate_bank(tmp_path / "a", seed)
    b = generate_bank(tmp_path / "b", seed)
    for name in [p.name for p in a.iterdir()]:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_different_seed_changes_bytes_but_stays_exact(tmp_path: Path) -> None:
    a = generate_bank(tmp_path / "a", seed=7)
    b = generate_bank(tmp_path / "b", seed=11)
    assert any((a / name).read_bytes() != (b / name).read_bytes() for name in ("hr_employees.csv",))
    for directory in (a, b):
        findings = run_access_rules(load(directory))
        truth = json.loads((directory / "ground_truth.json").read_text())
        got = {(f.rule_id, f.entity) for f in findings}
        assert got == a_issues(truth), directory
        assert dict(Counter(f.rule_id for f in findings)) == SPEC_COUNTS


def test_volume_and_structure(injected_dir: Path) -> None:
    data = load(injected_dir)
    assert 700 <= len(data.employees) <= 900
    assert set(data.accounts["system"].unique()) >= {"CORE", "PAY", "GL", "TRADE", "AD"}
    role_graph = nx.DiGraph(
        (u, v)
        for u, v in data.role_graph.edges()
        if data.role_graph.nodes[u]["kind"] == "role" and data.role_graph.nodes[v]["kind"] == "role"
    )
    # At least one 3-level inheritance chain (3 role nodes, 2 edges).
    assert nx.dag_longest_path_length(role_graph) >= 2


# ---------------------------------------------------------------------------
# Near misses (spec section 5.4) -- must exist in the clean AND injected bank
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("directory_fixture", ["clean_dir", "injected_dir"])
def test_near_misses(request, directory_fixture: str) -> None:
    directory: Path = request.getfixturevalue(directory_fixture)
    data = load(directory)
    emps = data.employees
    accts = data.accounts

    # 1: a leaver whose account is disabled.
    leavers = set(emps.index[emps["has_left"]])
    assert any(
        row["employee_id"] in leavers and row["status"] == "disabled"
        for _, row in accts[accts["account_type"] == "personal"].iterrows()
    )

    # 2: a leaver inside the grace period (exit_date == as_of, still active).
    grace = [e for e, d in emps["exit_date"].items() if d == AS_OF]
    assert grace and all(
        accts.at[a, "status"] == "active" for a in accts.index[accts["employee_id"].isin(grace)]
    )

    # 3: an active personal account last used exactly 89 days before as_of.
    target = AS_OF - timedelta(days=89)
    assert any(
        row["last_login"].date() == target
        for _, row in accts.iterrows()
        if row["status"] == "active" and row["account_type"] == "personal"
    )

    # 4: a non-interactive generic account.
    generic = accts[accts["account_type"] == "generic"]
    assert (generic["interactive"] == False).any()  # noqa: E712

    # 5: a technical account that never logged in.
    tech = accts[accts["account_type"] == "technical"]
    assert tech["last_login"].isna().any()

    # 6: a privileged role reviewed in a campaign that ended 300 days ago.
    target = AS_OF - timedelta(days=300)
    camps = data.campaigns[data.campaigns["end_date"] == target]
    assert not camps.empty
    roles = data.roles.index[data.roles["privileged"]]
    assert any(
        row["role_id"] in set(roles)
        for _, row in data.decisions[
            data.decisions["campaign_id"].isin(set(camps["campaign_id"]))
        ].iterrows()
    )

    # 7: an employee with two functions that do not conflict.
    functions = employee_functions(data)
    non_conflict = {"PAYMENT_CREATE", "BENEFICIARY_MAINTAIN"}
    flagged = {f.entity.split(":")[0] for f in run_access_rules(data) if f.rule_id == "A-05"}
    assert any(non_conflict <= set(held) and emp not in flagged for emp, held in functions.items())

    # 8: a reviewer with >= 20 decisions that are all slow enough.
    grouped = data.decisions.groupby(["campaign_id", "reviewer_employee_id"])
    slow_reviewers = 0
    for _, group in grouped:
        duration = (group["decided_at"] - group["item_shown_at"]).dt.total_seconds()
        if len(group) >= 20 and (duration >= 5).all():
            slow_reviewers += 1
    assert slow_reviewers >= 1

    # 9: a rare role held in a peer group below min_peers (5).
    emp_peer = {
        eid: (org, func)
        for eid, org, func in zip(
            emps["employee_id"], emps["org_unit"], emps["job_function"], strict=True
        )
    }
    group_sizes = Counter(emp_peer.values())
    personal_active = accts[(accts["account_type"] == "personal") & (accts["status"] == "active")]
    acct_emp = personal_active["employee_id"].to_dict()
    holders = {
        (acct_emp[row["account_id"]], row["role_id"])
        for _, row in data.active_assignments.iterrows()
        if row["account_id"] in acct_emp
    }
    # Orphan accounts (A-07) point to employee ids that are not in HR, so
    # they have no peer group. Skip them instead of looking them up.
    assert any(group_sizes[emp_peer[emp]] < 5 for emp, _role in sorted(holders) if emp in emp_peer)


def test_generation_and_rules_are_fast(tmp_path: Path) -> None:
    start = time.monotonic()
    directory = generate_bank(tmp_path / "fast", seed=13)
    findings = run_access_rules(load(directory))
    elapsed = time.monotonic() - start
    assert elapsed < 20.0
    truth = json.loads((directory / "ground_truth.json").read_text())
    got = {(f.rule_id, f.entity) for f in findings}
    assert got == a_issues(truth)


def _early_roles(directory: Path) -> list[tuple[str, str, str, str]]:
    """Role assignments that start before the owner joined.

    Accounts without an HR owner (technical, generic, A-07 orphans) are checked
    against their creation date instead.
    """
    import csv

    hr = {r["employee_id"]: r for r in csv.DictReader(open(directory / "hr_employees.csv"))}
    accounts = {r["account_id"]: r for r in csv.DictReader(open(directory / "accounts.csv"))}
    early = []
    for row in csv.DictReader(open(directory / "account_roles.csv")):
        account = accounts[row["account_id"]]
        if account["employee_id"] in hr:
            bounds = [hr[account["employee_id"]]["entry_date"]]
        else:
            bounds = [account["created_at"][:10]] if account["created_at"] else []
        for bound in bounds:
            if row["valid_from"] < bound:
                early.append((row["account_id"], row["role_id"], row["valid_from"], bound))
    return early


@pytest.mark.parametrize("seed", [2, 7, 11])
@pytest.mark.parametrize("inject", [True, False])
def test_no_role_starts_before_account_or_entry(tmp_path: Path, seed: int, inject: bool) -> None:
    """A reviewer would ask how someone held a role before joining; the data must not invite it."""
    directory = generate_bank(tmp_path / "bank", seed=seed, inject=inject)
    assert _early_roles(directory) == []
