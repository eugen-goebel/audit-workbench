"""Tests for the synthetic change data of module C (spec section 4).

The detection rules of ``audit_workbench.change`` are the acceptance oracle:
the clean bank must give zero findings, the injected bank exactly the
ground truth. Near misses are asserted to be *present* in the data, so a
near miss that silently disappeared from the generator fails here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.access.rules import run_access_rules
from audit_workbench.change.data import ChangeData, load_change_data
from audit_workbench.change.rules import run_change_rules
from audit_workbench.core.findings import Finding
from audit_workbench.synth import generate_bank
from audit_workbench.synth.bank import ROLE_MAP

AS_OF = date(2026, 9, 30)
PERIOD_START = date(2025, 10, 1)
SPEC_COUNTS = {"C-01": 6, "C-02": 5, "C-03": 4, "C-04": 4, "C-05": 4, "C-06": 3}
CHANGE_FILES = ["changes.csv", "change_approvals.csv", "commits.csv", "deployments.csv"]
BUSINESS_UNIT = {
    "PAY": "Zahlungsverkehr",
    "CORE": "Kredit",
    "GL": "Rechnungswesen",
    "TRADE": "Handel",
    "AD": "IT-Betrieb",
}

Key = tuple[str, str]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class Bank:
    """One generated bank with its loaded data, findings and ground truth."""

    def __init__(self, directory: Path, seed: int, inject: bool) -> None:
        self.dir = directory
        self.seed = seed
        self.inject = inject
        self.data: ChangeData = load_change_data(directory)
        self.findings: list[Finding] = run_change_rules(self.data)
        self.truth: dict = json.loads((directory / "ground_truth.json").read_text())
        self.found: set[Key] = {(f.rule_id, f.entity) for f in self.findings}
        self.want: set[Key] = {
            (i["rule_id"], i["entity"]) for i in self.truth["issues"] if i["rule_id"][0] == "C"
        }

    def finding(self, rule_id: str, entity: str) -> Finding:
        return next(f for f in self.findings if f.rule_id == rule_id and f.entity == entity)


@pytest.fixture(scope="module", params=[(7, False), (7, True), (11, False), (11, True)], ids=str)
def bank(request, tmp_path_factory) -> Bank:
    seed, inject = request.param
    directory = generate_bank(tmp_path_factory.mktemp(f"c{seed}{int(inject)}"), seed, inject=inject)
    return Bank(directory, seed, inject)


@pytest.fixture(scope="module")
def inj7(tmp_path_factory) -> Bank:
    return Bank(generate_bank(tmp_path_factory.mktemp("inj7"), 7), 7, True)


@pytest.fixture(scope="module")
def inj11(tmp_path_factory) -> Bank:
    return Bank(generate_bank(tmp_path_factory.mktemp("inj11"), 11), 11, True)


@pytest.fixture(scope="module")
def clean7(tmp_path_factory) -> Bank:
    return Bank(generate_bank(tmp_path_factory.mktemp("clean7"), 7, inject=False), 7, False)


@pytest.fixture(scope="module")
def clean11(tmp_path_factory) -> Bank:
    return Bank(generate_bank(tmp_path_factory.mktemp("clean11"), 11, inject=False), 11, False)


@pytest.fixture(params=["inj7", "inj11"])
def injected(request) -> Bank:
    return request.getfixturevalue(request.param)


@pytest.fixture(params=["clean7", "clean11"])
def clean(request) -> Bank:
    return request.getfixturevalue(request.param)


def read_csv(directory: Path, name: str) -> list[dict[str, str]]:
    with (directory / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------


def test_clean_bank_has_no_findings(clean: Bank) -> None:
    assert clean.findings == []
    assert clean.want == set()


def test_injected_findings_equal_ground_truth(injected: Bank) -> None:
    missing, extra = sorted(injected.want - injected.found), sorted(injected.found - injected.want)
    assert injected.found == injected.want, f"missing: {missing}, not in truth: {extra}"


def test_counts_match_spec_table(injected: Bank) -> None:
    counts = Counter(rule for rule, _ in injected.want)
    assert dict(sorted(counts.items())) == SPEC_COUNTS
    assert sum(counts.values()) == 26
    assert dict(sorted(Counter(f.rule_id for f in injected.findings).items())) == SPEC_COUNTS


def test_ground_truth_payload(bank: Bank) -> None:
    truth = bank.truth
    assert truth["change_as_of"] == "2026-09-30"
    assert truth["journal_as_of"] == "2026-02-28" and truth["as_of"] == "2026-09-30"
    keys = [(i["rule_id"], i["entity"]) for i in truth["issues"]]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    c_issues = [i for i in truth["issues"] if i["rule_id"][0] == "C"]
    if not bank.inject:
        assert c_issues == []
    for issue in c_issues:
        assert set(issue) == {"rule_id", "entity", "note"}
        assert issue["note"] and issue["entity"] in issue["note"]


def test_module_a_and_b_ground_truth_unchanged_in_size(injected: Bank) -> None:
    counts = Counter(i["rule_id"][0] for i in injected.truth["issues"])
    assert counts["A"] == 55 and counts["B"] == 42 and counts["C"] == 26


def test_sub_cases_of_the_spec_table(injected: Bank) -> None:
    f = injected.finding
    by_rule: dict[str, list[Finding]] = {}
    for finding in injected.findings:
        by_rule.setdefault(finding.rule_id, []).append(finding)

    c01 = by_rule["C-01"]
    triggers = Counter(t for x in c01 for t in x.details["triggers"])
    assert triggers == {"missing": 2, "unknown": 1, "system_mismatch": 1, "not_approved": 2}
    assert all(len(x.details["triggers"]) == 1 for x in c01)
    cancelled = [
        x
        for x in c01
        if x.details["triggers"] == ["not_approved"]
        and injected.data.changes.at[x.details["change_id"], "status"] == "cancelled"
    ]
    assert len(cancelled) == 1

    c02 = by_rule["C-02"]
    assert Counter((x.details["method"], x.details["via_second_account"]) for x in c02) == {
        ("manual", False): 2,
        ("manual", True): 1,
        ("pipeline", False): 2,
    }

    c03 = by_rule["C-03"]
    assert Counter(x.details["trigger"] for x in c03) == {"missing": 2, "late": 2}
    assert (
        sorted(x.details["days_after_deployment"] for x in c03 if x.details["trigger"] == "late")[0]
        == 8
    )
    assert all(x.details["post_days"] == 7 for x in c03)

    c05 = by_rule["C-05"]
    assert Counter(x.details["role"] for x in c05) == {"requester": 2, "author": 2}
    assert Counter(x.details["approval_stage"] for x in c05) == {"pre": 3, "post": 1}

    c06 = by_rule["C-06"]
    assert Counter(x.details["trigger"] for x in c06) == {
        "missing": 1,
        "after_deploy": 1,
        "self_tested": 1,
    }
    assert len(by_rule["C-04"]) == 4
    assert f("C-04", by_rule["C-04"][0].entity).details["hours_before_approval"] > 0


def test_c01_and_c02_entities_sit_on_single_deployment_changes(injected: Bank) -> None:
    data = injected.data
    for finding in injected.findings:
        if finding.rule_id not in ("C-01", "C-02"):
            continue
        change_id = finding.details.get("change_id")
        if finding.rule_id == "C-02":
            dep = next(d for d in data.prod_deployments if d.deployment_id == finding.entity)
            change_id = dep.change_id
        if change_id and change_id in data.changes.index:
            assert len(data.deployments_by_change[change_id]) == 1, finding.entity


def test_deliberate_overlaps_are_in_the_ground_truth(injected: Bank) -> None:
    """Changes with two characteristics: C-04 and C-06, C-02 and C-05 (spec: some carry two)."""
    data = injected.data
    c04 = {e for r, e in injected.want if r == "C-04"}
    c06 = {e for r, e in injected.want if r == "C-06"}
    c05 = {e for r, e in injected.want if r == "C-05"}
    c02_changes = {
        d.change_id for d in data.prod_deployments if ("C-02", d.deployment_id) in injected.want
    }
    assert c04 & c06 and c02_changes & c05
    assert len(c04 & c06) == 1 and len(c02_changes & c05) == 1


# ---------------------------------------------------------------------------
# near misses: each must exist in the data and must not be flagged
# ---------------------------------------------------------------------------


def _prod_ids(data: ChangeData, change_id: str) -> list[str]:
    return [d.deployment_id for d in data.deployments_by_change.get(change_id, [])]


def near_standard_without_approval(data: ChangeData) -> list[set[Key]]:
    """A standard change deployed without any approval (C-01)."""
    return [
        {("C-01", dep) for dep in _prod_ids(data, cid)}
        for cid in data.population
        if data.changes.at[cid, "change_type"] == "standard" and not data.approvals.get(cid)
    ]


def near_rejected_then_approved(data: ChangeData) -> list[set[Key]]:
    """Rejected first, approved later and before the deployment (C-01, C-04)."""
    out = []
    for cid in data.population:
        if data.changes.at[cid, "change_type"] != "normal":
            continue
        pre = [a for a in data.approvals.get(cid, []) if a.stage == "pre"]
        first = data.first_prod_deployment(cid)
        assert first is not None
        decisions = [a.decision for a in pre]
        if (
            "rejected" in decisions
            and decisions[-1] == "approved"
            and pre[-1].decided_ts < first.deployed_ts
        ):
            out.append({("C-04", cid)} | {("C-01", d) for d in _prod_ids(data, cid)})
    return out


def near_same_timestamp(data: ChangeData) -> list[set[Key]]:
    """Approval and first production deployment at the same timestamp (C-04)."""
    out = []
    for cid in data.population:
        approval = data.effective_approval(cid)
        first = data.first_prod_deployment(cid)
        if approval and data.changes.at[cid, "change_type"] == "normal":
            assert first is not None
            if approval.decided_ts == first.deployed_ts:
                out.append({("C-04", cid)})
    return out


def near_post_approved_on_day_seven(data: ChangeData) -> list[set[Key]]:
    """An emergency change post-approved exactly 7 days after deployment (C-03)."""
    out = []
    for cid in data.population:
        post = data.first_post_approval(cid)
        first = data.first_prod_deployment(cid)
        if post and first and (post.decided_ts.date() - first.deployed_ts.date()).days == 7:
            out.append({("C-03", cid)})
    return out


def near_emergency_deployed_three_days_before_as_of(data: ChangeData) -> list[set[Key]]:
    """An emergency change deployed 3 days before as_of, post-approval still open (C-03)."""
    out = []
    for cid in data.population:
        first = data.first_prod_deployment(cid)
        if (
            data.changes.at[cid, "change_type"] == "emergency"
            and first
            and first.deployed_ts.date() == AS_OF - timedelta(days=3)
            and data.first_post_approval(cid) is None
        ):
            out.append({("C-03", cid)})
    return out


def near_developer_deploys_own_change_to_test(data: ChangeData) -> list[set[Key]]:
    """A developer deploying their own change to test (C-02 is prod only)."""
    df = data.deployments
    df = df[(df["environment"] == "test") & (df["change_id"] != "")]
    return [
        {("C-02", row.deployment_id)}
        for row in df.itertuples()
        if row.deploying_employee_id in data.commit_authors.get(row.change_id, frozenset())
    ]


def near_developer_deploys_unrelated_change(data: ChangeData) -> list[set[Key]]:
    """A developer who wrote commits for change X deploys an unrelated change Y (C-02)."""
    wrote_any = set(data.commits.loc[data.commits["change_id"] != "", "author_employee_id"])
    out = []
    for dep in data.prod_deployments:
        employee = dep.deploying_employee_id
        if not dep.change_id or employee is None:
            continue
        unit = data.employees.at[employee, "org_unit"]
        if (
            unit == "Entwicklung"
            and employee in wrote_any
            and employee not in data.commit_authors.get(dep.change_id, frozenset())
        ):
            out.append({("C-02", dep.deployment_id)})
    return out


def near_pipeline_release_by_team_colleague(data: ChangeData) -> list[set[Key]]:
    """A pipeline deployment released by a team colleague of the author (C-02)."""
    out = []
    for dep in data.prod_deployments:
        employee = dep.deploying_employee_id
        authors = data.commit_authors.get(dep.change_id, frozenset())
        if dep.method != "pipeline" or employee is None or not authors or employee in authors:
            continue
        emp = data.employees.loc[employee]
        if emp["org_unit"] == "Entwicklung" and any(
            data.employees.at[a, "org_unit"] == emp["org_unit"]
            and data.employees.at[a, "job_function"] == emp["job_function"]
            for a in authors
        ):
            out.append({("C-02", dep.deployment_id)})
    return out


def near_low_risk_without_signoff(data: ChangeData) -> list[set[Key]]:
    """A low-risk normal change without test sign-off (C-06 covers medium and high)."""
    return [
        {("C-06", cid)}
        for cid in data.population
        if data.changes.at[cid, "change_type"] == "normal"
        and data.changes.at[cid, "risk"] == "low"
        and data.changes.at[cid, "test_signoff_ts"] is None
    ]


def near_failed_then_redeployed(data: ChangeData) -> list[set[Key]]:
    """A failed deployment followed by a successful redeployment (C-03 to C-06 once)."""
    out = []
    for cid in data.population:
        deps = data.deployments_by_change[cid]
        if len(deps) >= 2 and deps[0].result != "success" and deps[-1].result == "success":
            keys: set[Key] = {(rule, cid) for rule in ("C-03", "C-04", "C-05", "C-06")}
            keys |= {(rule, d.deployment_id) for d in deps for rule in ("C-01", "C-02")}
            out.append(keys)
    return out


NEAR_MISSES = [
    (near_standard_without_approval, 100),
    (near_rejected_then_approved, 10),
    (near_same_timestamp, 2),
    (near_post_approved_on_day_seven, 2),
    (near_emergency_deployed_three_days_before_as_of, 1),
    (near_developer_deploys_own_change_to_test, 100),
    (near_developer_deploys_unrelated_change, 3),
    (near_pipeline_release_by_team_colleague, 3),
    (near_low_risk_without_signoff, 50),
    (near_failed_then_redeployed, 50),
]


@pytest.mark.parametrize("detector, minimum", NEAR_MISSES, ids=[n.__name__ for n, _ in NEAR_MISSES])
def test_near_miss_exists_and_is_not_flagged(bank: Bank, detector, minimum: int) -> None:
    candidates = detector(bank.data)
    # candidates that are not themselves injected issues must be absent from the findings
    untouched = [keys for keys in candidates if keys.isdisjoint(bank.want)]
    assert len(untouched) >= minimum, f"{detector.__name__}: only {len(untouched)} untouched"
    for keys in untouched:
        assert keys.isdisjoint(bank.found), f"{detector.__name__} flagged: {keys & bank.found}"
    if not bank.inject:
        assert len(candidates) == len(untouched)


# ---------------------------------------------------------------------------
# volumes
# ---------------------------------------------------------------------------


def test_volumes_are_roughly_as_in_the_spec(bank: Bank) -> None:
    data = bank.data
    changes = data.changes
    assert 1700 <= len(changes) <= 1900
    shares = changes["change_type"].value_counts(normalize=True)
    assert 0.62 <= shares["normal"] <= 0.68
    assert 0.27 <= shares["standard"] <= 0.33
    assert 0.03 <= shares["emergency"] <= 0.07
    assert 5400 <= len(data.commits) <= 6600
    prod = data.deployments[data.deployments["environment"] == "prod"]
    test = data.deployments[data.deployments["environment"] == "test"]
    assert 1650 <= len(prod) <= 2050
    assert 2700 <= len(test) <= 3300
    assert 0.65 <= (prod["method"] == "pipeline").mean() <= 0.75
    assert set(changes["status"]) == {"closed", "cancelled", "in_progress"}
    assert set(prod["result"]) == {"success", "failed", "rolled_back"}
    # Change failure rate in a realistic band, not every second attempt.
    assert 0.08 <= (prod["result"] != "success").mean() <= 0.20
    assert len(prod) == len(data.prod_deployments)  # everything is inside the period


def test_all_activity_is_inside_the_period(bank: Bank) -> None:
    data = bank.data
    start, end = datetime(2025, 10, 1), datetime(2026, 9, 30, 23, 59, 59)
    stamps = list(data.changes["created_ts"]) + list(data.commits["committed_ts"])
    stamps += list(data.deployments["deployed_ts"])
    stamps += [a.decided_ts for items in data.approvals.values() for a in items]
    stamps += [t for t in data.changes["test_signoff_ts"] if t is not None]
    assert min(stamps) >= start and max(stamps) <= end


def test_ids_are_unique_and_links_resolve(bank: Bank) -> None:
    data = bank.data
    assert data.deployments["deployment_id"].is_unique
    assert data.commits["commit_sha"].is_unique
    assert data.commits["commit_sha"].str.fullmatch("[0-9a-f]{40}").all()
    known = set(data.changes.index)
    linked = data.commits.loc[data.commits["change_id"] != "", "change_id"]
    assert set(linked) <= known
    assert set(data.evidence["change_approvals"].frame["change_id"]) <= known
    unlinked = set(data.deployments["change_id"]) - known - {""}
    assert len(unlinked) == (1 if bank.inject else 0)  # the one invented change id of C-01


# ---------------------------------------------------------------------------
# cast
# ---------------------------------------------------------------------------


def _employees(data: ChangeData, ids) -> list[tuple[str, str]]:
    return sorted(
        {(data.employees.at[e, "org_unit"], data.employees.at[e, "job_function"]) for e in ids}
    )


def test_cast_comes_from_the_documented_groups(bank: Bank) -> None:
    data = bank.data
    emp = data.employees
    commits = data.commits
    assert {emp.at[e, "org_unit"] for e in commits["author_employee_id"]} == {"Entwicklung"}

    self_tested = {
        f.entity
        for f in bank.findings
        if f.rule_id == "C-06" and f.details["trigger"] == "self_tested"
    }
    for change in data.changes.itertuples():
        unit = BUSINESS_UNIT[change.system]
        assert _employees(data, [change.requester_employee_id]) == [(unit, "Teamleitung")]
        if change.tester_employee_id and change.change_id not in self_tested:
            assert _employees(data, [change.tester_employee_id]) == [(unit, "Analyse")]
        elif change.tester_employee_id:  # injected C-06 self_tested: the tester is a developer
            assert emp.at[change.tester_employee_id, "org_unit"] == "Entwicklung"

    approvers = _employees(
        data, [a.approver_employee_id for items in data.approvals.values() for a in items]
    )
    # the CAB, plus the requesters / authors that C-05 injects as approvers
    assert ("IT-Betrieb", "Teamleitung") in approvers
    assert len(approvers) <= (5 if bank.inject else 1)

    test = data.deployments[data.deployments["environment"] == "test"]
    assert set(test["method"]) == {"manual"}
    assert {emp.at[e, "org_unit"] for e in test["deploying_employee_id"]} == {"Entwicklung"}
    for row in test.itertuples():
        assert data.accounts.at[row.account_id, "account_type"] == "personal"


def test_production_deployers_and_the_pipeline_account(bank: Bank) -> None:
    data = bank.data
    prod = data.deployments[data.deployments["environment"] == "prod"]
    pipeline = prod[prod["method"] == "pipeline"]
    manual = prod[prod["method"] == "manual"]
    accounts = set(pipeline["account_id"])
    assert len(accounts) == 1
    account = data.accounts.loc[next(iter(accounts))]
    assert (account["system"], account["account_type"]) == ("AD", "technical")
    assert account["status"] == "active" and account["created_at"] < "2025-10-01"

    releasers = Counter(
        (data.employees.at[e, "org_unit"], data.employees.at[e, "job_function"])
        for e in pipeline["released_by_employee_id"]
    )
    assert releasers[("IT-Betrieb", "Sachbearbeitung")] >= 0.95 * len(pipeline)
    assert set(releasers) <= {
        ("IT-Betrieb", "Sachbearbeitung"),
        ("Entwicklung", "Sachbearbeitung"),
        ("Entwicklung", "Analyse"),
        ("Entwicklung", "Administration"),
    }
    operators = Counter(
        (data.employees.at[e, "org_unit"], data.employees.at[e, "job_function"])
        for e in manual["deploying_employee_id"].dropna()
    )
    assert operators[("IT-Betrieb", "Administration")] >= 0.95 * len(manual)
    for row in manual.itertuples():
        assert data.accounts.at[row.account_id, "account_type"] == "personal"


def test_only_employees_active_over_the_whole_period_take_part(bank: Bank) -> None:
    data = bank.data
    people: set[str] = set(data.changes["requester_employee_id"]) | set(
        data.changes["tester_employee_id"]
    )
    people |= set(data.commits["author_employee_id"])
    people |= {a.approver_employee_id for items in data.approvals.values() for a in items}
    people |= set(data.deployments["deploying_employee_id"].dropna())
    people.discard("")
    hr = data.employees.loc[sorted(people)]
    assert (hr["exit_date"] == "").all()
    assert (hr["entry_date"] < PERIOD_START.isoformat()).all()


def test_nobody_in_change_data_is_part_of_an_a_or_b_ground_truth_entry(bank: Bank) -> None:
    data = bank.data
    entities = {i["entity"] for i in bank.truth["issues"] if i["rule_id"][0] in "AB"}
    accounts = data.accounts
    subjects = entities | {accounts.at[e, "employee_id"] for e in entities if e in accounts.index}
    people: set[str] = set(data.changes["requester_employee_id"]) | set(
        data.changes["tester_employee_id"]
    )
    people |= set(data.commits["author_employee_id"])
    people |= {a.approver_employee_id for items in data.approvals.values() for a in items}
    people |= set(data.deployments["deploying_employee_id"].dropna())
    people.discard("")
    subjects.discard("")
    assert not (people & subjects)
    assert "AD-" not in "".join(sorted(set(data.deployments["account_id"]) & subjects))


# ---------------------------------------------------------------------------
# second AD account (C-02) and module A
# ---------------------------------------------------------------------------


def test_second_ad_account_is_clean_for_every_a_rule(injected: Bank) -> None:
    finding = next(f for f in injected.findings if f.details.get("via_second_account"))
    employee, account_id = finding.details["deploying_employee_id"], finding.details["account_id"]
    rows = read_csv(injected.dir, "accounts.csv")
    account = next(r for r in rows if r["account_id"] == account_id)
    mine = [r for r in rows if r["employee_id"] == employee and r["system"] == "AD"]
    assert len(mine) == 2 and injected.data.primary_ad_account[employee] != account_id
    assert account["account_type"] == "personal" and account["status"] == "active"
    assert account["interactive"] == "true"
    assert account_id == max(r["account_id"] for r in rows if r["system"] == "AD")  # appended last

    hr = injected.data.employees.loc[employee]
    standard = ROLE_MAP[(hr["org_unit"], hr["job_function"])]
    roles = [
        r for r in read_csv(injected.dir, "account_roles.csv") if r["account_id"] == account_id
    ]
    assert [r["role_id"] for r in roles] == [standard]
    assert hr["org_unit"] == "Entwicklung" and hr["exit_date"] == ""
    login = datetime.fromisoformat(account["last_login"]).date()
    assert 0 <= (AS_OF - login).days <= 30
    decisions = [
        r for r in read_csv(injected.dir, "recert_decisions.csv") if r["account_id"] == account_id
    ]
    assert len(decisions) == 1 and decisions[0]["decision"] == "keep"
    assert decisions[0]["reviewer_employee_id"] != employee

    # the module A rules still return exactly their ground truth
    data = AccessData(evidence=load_access_evidence(injected.dir), as_of=AS_OF)
    got = {(f.rule_id, f.entity) for f in run_access_rules(data)}
    want = {(i["rule_id"], i["entity"]) for i in injected.truth["issues"] if i["rule_id"][0] == "A"}
    assert got == want


def test_no_second_account_without_injection(clean7: Bank, inj7: Bank) -> None:
    clean_rows = read_csv(clean7.dir, "accounts.csv")
    inj_rows = read_csv(inj7.dir, "accounts.csv")
    ad_personal = Counter(
        r["employee_id"]
        for r in clean_rows
        if r["system"] == "AD" and r["account_type"] == "personal"
    )
    assert max(ad_personal.values()) == 1
    assert len(inj_rows) >= len(clean_rows)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def _digest(directory: Path, names: list[str]) -> dict[str, str]:
    return {n: hashlib.sha256((directory / n).read_bytes()).hexdigest() for n in names}


@pytest.mark.parametrize("seed", [7, 11])
def test_same_seed_same_bytes(tmp_path: Path, seed: int) -> None:
    a = generate_bank(tmp_path / "a", seed)
    b = generate_bank(tmp_path / "b", seed)
    names = [*CHANGE_FILES, "ground_truth.json", "accounts.csv"]
    assert _digest(a, names) == _digest(b, names)


def test_different_seed_gives_different_change_data(inj7: Bank, inj11: Bank) -> None:
    assert _digest(inj7.dir, CHANGE_FILES) != _digest(inj11.dir, CHANGE_FILES)


def test_identical_across_processes_with_different_hash_seeds(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from audit_workbench.synth import generate_bank\n"
        "generate_bank(sys.argv[1], seed=7)\n"
    )
    root = Path(__file__).resolve().parents[2]
    digests = []
    for hash_seed in ("1", "999"):
        target = tmp_path / f"hash{hash_seed}"
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        subprocess.run([sys.executable, "-c", script, str(target)], check=True, env=env, cwd=root)
        digests.append(_digest(target, [*CHANGE_FILES, "ground_truth.json"]))
    assert digests[0] == digests[1]


#: hashes of the module B files and ground-truth entries as generated on main,
#: before module C existed
MODULE_B_BASELINE = {
    7: {
        "gl_accounts.csv": "a5b49277edaa38be8ea2260ebeba03802c03405a8c1fc0fbe0c95a330bc09b5b",
        "journal_entries.csv": "aab3a29c2e715f84481c1dbde390b7eb72836f9fd9088e0ef4b67914452daed6",
        "journal_lines.csv": "c886be29a89a7e17d8179f24bf25f378d5c34dd537f80737f8dfb893f70de1cd",
        "close_calendar.csv": "fb3b2b802e12c6652ac66033eafc59a7c8b2362d0e143366f6cb7b617cf3f417",
        "approval_limits.csv": "c19ee8502a5f55870f65e7adc32fe60e0b1434acb8485fcb3500a1d29ffb1905",
        "b_issues": "84209c6b9bd1a3de6c1610661a5e558c818ae55cae0732b0cec6ff62936fc52e",
    },
    11: {
        "gl_accounts.csv": "a5b49277edaa38be8ea2260ebeba03802c03405a8c1fc0fbe0c95a330bc09b5b",
        "journal_entries.csv": "eb6cdcc58cdf666fb60cdf70b3219a3d216e6e722276afd6484c12bfd337a840",
        "journal_lines.csv": "5707cbb49502c17964d4540ac14b8dbcd9f307d0bb778f84fcae5f73a6e431f8",
        "close_calendar.csv": "fb3b2b802e12c6652ac66033eafc59a7c8b2362d0e143366f6cb7b617cf3f417",
        "approval_limits.csv": "c19ee8502a5f55870f65e7adc32fe60e0b1434acb8485fcb3500a1d29ffb1905",
        "b_issues": "c8f1764092b4cc6b8d3077675ba713ba8021eb2d8f17c215d5b7187274ce14be",
    },
}


def test_module_b_is_byte_identical_to_main(injected: Bank) -> None:
    expected = MODULE_B_BASELINE[injected.seed]
    for name, digest in expected.items():
        if name == "b_issues":
            continue
        got = hashlib.sha256((injected.dir / name).read_bytes()).hexdigest()
        assert got == digest, name
    b_issues = [i for i in injected.truth["issues"] if i["rule_id"].startswith("B-")]
    got = hashlib.sha256(json.dumps(b_issues, sort_keys=True).encode()).hexdigest()
    assert got == expected["b_issues"]
