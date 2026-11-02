"""Tests for cross-module rules X-01 to X-03 (spec docs/design/phase-4-crosslink.md, sections 1, 3, 6).

Every trigger has a test, and so does every near miss of spec section 6 that
small data can express:

  X-01  rights start 2026-07-01, after the ledger year (risk, not exercised);
        the employee holding both functions approves another clerk's entry
  X-02  entry prepared on the first day of the preparer's role assignment
  X-03  a leaver's commit on the exit date itself; actions only before the exit date

The bank is built from the three mini builders (see `builders.py`) and read
through the real loaders.
"""

from __future__ import annotations

from datetime import date

import pytest

from audit_workbench.core.findings import Finding, Severity
from audit_workbench.crosslink import (
    CROSSLINK_RULES,
    ActionWithoutEntitlement,
    ActivityAfterExit,
    CrosslinkData,
    SodExercised,
    run_crosslink_rules,
)
from tests.crosslink.builders import World, gl_world

LEAVER_EXIT = "2025-11-15"  # `gl_world` leaver LEAV


def _entry(
    world: World,
    *,
    preparer: str = "GL-E1",
    approver: str = "GL-E1",
    amount: str = "100.00",
    day: str = "2025-03-10",
    approved_day: str | None = None,
    doc_no: str | None = None,
    source: str = "manual",
) -> str:
    """A manual entry keyed at 10:00 and approved the same day at 11:00."""
    approved_ts = f"{approved_day or day}T11:00:00" if approver else ""
    return world.ledger.entry(
        doc_no,
        amount=amount,
        posting_date=day,
        entry_ts=f"{day}T10:00:00",
        preparer=preparer,
        approver=approver,
        approved_ts=approved_ts,
        source=source,
    )


def _both_rights(world: World, account: str = "GL-E1", since: str = "2024-12-01") -> None:
    world.access.assign(account, "R-POST", valid_from=since)
    world.access.assign(account, "R-APPR", valid_from=since)


def _run(world: World, tmp_path, rule_id: str | None = None, related=None) -> list[Finding]:
    loaded = world.load(tmp_path)
    findings = run_crosslink_rules(loaded.access, loaded.journal, loaded.change, related)
    return [f for f in findings if rule_id is None or f.rule_id == rule_id]


def _fake(rule_id: str, entity: str) -> Finding:
    return Finding(
        rule_id=rule_id,
        rule_version="1.0.0",
        severity=Severity.HIGH,
        entity=entity,
        description="stand-in for a module finding",
        evidence=["0" * 64],
    )


# ===========================================================================
# X-01  sod_exercised
# ===========================================================================


def test_x01_same_account_self_approval(tmp_path) -> None:
    world = gl_world()
    _both_rights(world)
    doc = _entry(world)

    [f] = _run(world, tmp_path, "X-01")

    assert f.entity == "E1:SOD-GL-01"
    assert f.severity == Severity.HIGH
    assert f.rule_version == "1.0.0"
    assert f.details["doc_nos"] == [doc]
    assert f.details["count"] == 1
    assert f.details["first_ts"] == f.details["last_ts"] == "2025-03-10T10:00:00"
    assert f.details["max_amount"] == "100.00"
    assert f.details["via_second_account"] is False
    assert f.details["held_on_as_of"] is True
    assert f.details["related"] == []


def test_x01_second_account_of_the_same_employee(tmp_path) -> None:
    world = gl_world()
    world.access.account("GL-E1B", employee_id="E1", system="GL")
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")
    world.access.assign("GL-E1B", "R-APPR", valid_from="2024-12-01")
    _entry(world, preparer="GL-E1", approver="GL-E1B")

    [f] = _run(world, tmp_path, "X-01")

    assert f.entity == "E1:SOD-GL-01"
    assert f.details["via_second_account"] is True


def test_x01_function_through_role_hierarchy(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-LEAD", valid_from="2024-12-01")  # contains R-POST
    world.access.assign("GL-E1", "R-APPR", valid_from="2024-12-01")
    _entry(world)

    assert [f.entity for f in _run(world, tmp_path, "X-01")] == ["E1:SOD-GL-01"]


def test_x01_aggregates_entries_of_one_employee(tmp_path) -> None:
    world = gl_world()
    world.access.account("GL-E1B", employee_id="E1", system="GL")
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")
    world.access.assign("GL-E1", "R-APPR", valid_from="2024-12-01")
    world.access.assign("GL-E1B", "R-APPR", valid_from="2024-12-01")
    late = _entry(world, doc_no="MJ-2025-000009", day="2025-06-02", amount="2500.50")
    early = _entry(world, doc_no="MJ-2025-000002", day="2025-04-01", amount="900.00")
    mixed = _entry(world, doc_no="MJ-2025-000005", day="2025-05-05", approver="GL-E1B")

    [f] = _run(world, tmp_path, "X-01")

    assert f.details["doc_nos"] == sorted([late, early, mixed])
    assert f.details["count"] == 3
    assert f.details["first_ts"] == "2025-04-01T10:00:00"
    assert f.details["last_ts"] == "2025-06-02T10:00:00"
    assert f.details["max_amount"] == "2500.50"
    assert f.details["via_second_account"] is True  # one entry used the second account


@pytest.mark.parametrize(
    ("amount", "severity"),
    [
        ("99999.99", Severity.HIGH),  # one cent below the manual limit: X-01 covers it, B-08 not
        ("100000.00", Severity.CRITICAL),  # at the limit
        ("250000.00", Severity.CRITICAL),
    ],
)
def test_x01_severity_raised_at_the_approval_limit(tmp_path, amount, severity) -> None:
    world = gl_world()
    _both_rights(world)
    _entry(world, amount=amount)

    [f] = _run(world, tmp_path, "X-01")

    assert f.severity == severity


def test_x01_one_large_entry_raises_severity_for_the_whole_finding(tmp_path) -> None:
    world = gl_world()
    _both_rights(world)
    _entry(world, amount="50.00", day="2025-03-10")
    _entry(world, amount="100000.00", day="2025-03-11")

    [f] = _run(world, tmp_path, "X-01")

    assert f.severity == Severity.CRITICAL
    assert f.details["count"] == 2


def test_x01_severity_comes_from_the_ruleset(tmp_path) -> None:
    ruleset = tmp_path / "sod.yaml"
    ruleset.write_text(
        "conflicts:\n"
        "  - id: SOD-GL-99\n"
        "    functions: [GL_POST, GL_APPROVE]\n"
        "    severity: critical\n"
        "    description: test\n"
        "  - id: SOD-GL-98\n"
        "    functions: [GL_POST, GL_VIEW]\n"  # GL_VIEW is not in the action table
        "    severity: critical\n"
        "    description: out of scope\n",
        encoding="utf-8",
    )
    world = gl_world()
    _both_rights(world)
    world.access.assign("GL-E1", "R-VIEW", valid_from="2024-12-01")
    _entry(world)
    loaded = world.load(tmp_path / "data")

    findings = SodExercised(ruleset_path=ruleset).run(
        CrosslinkData(loaded.access, loaded.journal, loaded.change)
    )

    assert [(f.entity, f.severity) for f in findings] == [("E1:SOD-GL-99", Severity.CRITICAL)]


def test_x01_held_on_as_of_is_false_when_the_rights_ended(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01", valid_to="2025-06-30")
    world.access.assign("GL-E1", "R-APPR", valid_from="2024-12-01", valid_to="2025-06-30")
    _entry(world)

    [f] = _run(world, tmp_path, "X-01")

    assert f.details["held_on_as_of"] is False  # A-05 would not report it


def test_x01_near_miss_rights_start_after_the_ledger_year(tmp_path) -> None:
    world = gl_world()
    _both_rights(world, since="2026-07-01")
    _entry(world)  # 2025: neither right held yet

    assert _run(world, tmp_path, "X-01") == []


def test_x01_near_miss_second_function_only_after_the_entry(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")
    world.access.assign("GL-E1", "R-APPR", valid_from="2025-03-11")  # day after the approval
    _entry(world)

    assert _run(world, tmp_path, "X-01") == []


def test_x01_near_miss_approves_another_clerks_entry(tmp_path) -> None:
    world = gl_world()
    _both_rights(world, "GL-E1")
    world.access.assign("GL-E2", "R-POST", valid_from="2024-12-01")
    _entry(world, preparer="GL-E2", approver="GL-E1")

    assert _run(world, tmp_path, "X-01") == []


def test_x01_near_miss_preparer_account_lacks_the_post_right(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-VIEW", valid_from="2024-12-01")  # B-10 style: view only
    world.access.assign("GL-E1", "R-APPR", valid_from="2024-12-01")
    _entry(world)

    assert _run(world, tmp_path, "X-01") == []


def test_x01_ignores_entries_without_approver_and_automatic_entries(tmp_path) -> None:
    world = gl_world()
    _both_rights(world)
    _entry(world, approver="")
    _entry(world, preparer="GL-BATCH", approver="", source="automatic")

    assert _run(world, tmp_path, "X-01") == []


def test_x01_separate_findings_per_employee(tmp_path) -> None:
    world = gl_world()
    _both_rights(world, "GL-E1")
    _both_rights(world, "GL-E2")
    _entry(world, preparer="GL-E2", approver="GL-E2")
    _entry(world, preparer="GL-E1", approver="GL-E1")

    assert [f.entity for f in _run(world, tmp_path, "X-01")] == [
        "E1:SOD-GL-01",
        "E2:SOD-GL-01",
    ]


# ===========================================================================
# X-02  action_without_entitlement
# ===========================================================================


def test_x02_preparer_without_any_right(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E2", "R-APPR", valid_from="2024-12-01")
    doc = _entry(world, preparer="GL-E1", approver="GL-E2")

    [f] = _run(world, tmp_path, "X-02")

    assert f.entity == doc
    assert f.severity == Severity.HIGH
    assert f.details["actions"] == ["je_prepare"]
    assert f.details["related"] == []
    assert "access" in f.description.lower()


def test_x02_approver_with_view_only(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")
    world.access.assign("GL-E2", "R-VIEW", valid_from="2024-12-01")
    doc = _entry(world, preparer="GL-E1", approver="GL-E2")

    [f] = _run(world, tmp_path, "X-02")

    assert f.entity == doc
    assert f.details["actions"] == ["je_approve"]


def test_x02_both_actions_in_one_finding(tmp_path) -> None:
    world = gl_world()
    doc = _entry(world, preparer="GL-E1", approver="GL-E1")  # no assignments at all

    [f] = _run(world, tmp_path, "X-02")

    assert f.entity == doc
    assert f.details["actions"] == ["je_prepare", "je_approve"]


def test_x02_wrong_function_is_not_enough(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-APPR", valid_from="2024-12-01")  # approve, but not post
    world.access.assign("GL-E2", "R-POST", valid_from="2024-12-01")  # post, but not approve
    doc = _entry(world, preparer="GL-E1", approver="GL-E2")

    [f] = _run(world, tmp_path, "X-02")

    assert f.entity == doc
    assert f.details["actions"] == ["je_prepare", "je_approve"]


def test_x02_assignment_ended_before_the_action(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01", valid_to="2025-03-09")
    doc = _entry(world, approver="")

    assert [f.entity for f in _run(world, tmp_path, "X-02")] == [doc]


def test_x02_assignment_starts_after_the_action(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2025-03-11")
    doc = _entry(world, approver="")

    assert [f.entity for f in _run(world, tmp_path, "X-02")] == [doc]


def test_x02_account_created_after_the_action(tmp_path) -> None:
    world = gl_world()
    world.access.account("GL-NEW", employee_id="E3", system="GL", created_at="2025-03-11T08:00:00")
    world.access.assign("GL-NEW", "R-POST", valid_from="2020-01-01")
    doc = _entry(world, preparer="GL-NEW", approver="")

    [f] = _run(world, tmp_path, "X-02")

    assert f.entity == doc
    assert f.details["actions"] == ["je_prepare"]


def test_x02_account_unknown_to_the_access_data(tmp_path) -> None:
    world = gl_world()
    doc = _entry(world, preparer="GL-GHOST", approver="")  # in no accounts file at all

    assert [f.entity for f in _run(world, tmp_path, "X-02")] == [doc]


def test_x02_near_miss_first_day_of_the_assignment(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2025-03-10")  # entry prepared that day
    _entry(world, day="2025-03-10", approver="")

    assert _run(world, tmp_path, "X-02") == []


def test_x02_near_miss_last_day_of_the_assignment(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01", valid_to="2025-03-10")
    _entry(world, approver="")

    assert _run(world, tmp_path, "X-02") == []


def test_x02_near_miss_account_created_on_the_action_day(tmp_path) -> None:
    world = gl_world()
    world.access.account("GL-NEW", employee_id="E3", system="GL", created_at="2025-03-10T16:00:00")
    world.access.assign("GL-NEW", "R-POST", valid_from="2025-03-10")
    _entry(world, preparer="GL-NEW", approver="")  # keyed at 10:00, account created 16:00

    assert _run(world, tmp_path, "X-02") == []  # the day counts, not the time


def test_x02_near_miss_function_through_hierarchy(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-LEAD", valid_from="2024-12-01")  # contains R-POST
    world.access.assign("GL-E2", "R-APPR", valid_from="2024-12-01")
    _entry(world, preparer="GL-E1", approver="GL-E2")

    assert _run(world, tmp_path, "X-02") == []


def test_x02_approval_day_decides_for_the_approver(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")
    world.access.assign("GL-E2", "R-APPR", valid_from="2025-03-11")  # starts the day after keying
    _entry(world, preparer="GL-E1", approver="GL-E2", day="2025-03-10", approved_day="2025-03-11")

    assert _run(world, tmp_path, "X-02") == []


def test_x02_unapproved_entry_tests_only_the_preparer(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")
    _entry(world, approver="")

    assert _run(world, tmp_path, "X-02") == []


def test_x02_ignores_automatic_entries(tmp_path) -> None:
    world = gl_world()
    _entry(world, preparer="GL-BATCH", approver="", source="automatic")

    assert _run(world, tmp_path, "X-02") == []


def test_x02_does_not_test_deployments(tmp_path) -> None:
    """Decision D1: no PROD_DEPLOY right, so deployments are no X-02 population."""
    world = gl_world()
    cid = world.changes.change()
    world.changes.deploy(
        cid, method="manual", account_id="AD-LEAV", deployed_ts="2025-11-01T20:00:00"
    )

    assert _run(world, tmp_path, "X-02") == []


# ===========================================================================
# X-03  activity_after_exit
# ===========================================================================


def _act_je_prepare(world: World, day: str) -> str:
    _entry(world, preparer="GL-LEAV", approver="", day=day, doc_no="MJ-2025-000077")
    return "MJ-2025-000077"


def _act_je_approve(world: World, day: str) -> str:
    _entry(
        world,
        preparer="GL-E1",
        approver="GL-LEAV",
        day="2025-11-10",
        approved_day=day,
        doc_no="MJ-2025-000078",
    )
    return "MJ-2025-000078"


def _act_prod_deploy_manual(world: World, day: str) -> str:
    cid = world.changes.change(author=None)
    return world.changes.deploy(
        cid,
        method="manual",
        account_id="AD-LEAV",
        deployed_ts=f"{day}T20:00:00",
        deployment_id="DEP-LEAV1",
    )


def _act_prod_deploy_pipeline(world: World, day: str) -> str:
    cid = world.changes.change(author=None)
    return world.changes.deploy(
        cid,
        method="pipeline",
        released_by="LEAV",
        deployed_ts=f"{day}T20:00:00",
        deployment_id="DEP-LEAV2",
    )


def _act_commit(world: World, day: str) -> str:
    cid = world.changes.change(author=None)
    return world.changes.commit(cid, author="LEAV", committed_ts=f"{day}T09:00:00", sha="a" * 40)


def _act_change_approval(world: World, day: str) -> str:
    return world.changes.change(
        "CHG-LEAV1", author=None, approver="LEAV", approved_ts=f"{day}T10:00:00"
    )


def _act_recert(world: World, day: str) -> str:
    world.access.campaign("CAMP-1")
    world.access.decision(
        "CAMP-1",
        "GL-E1",
        "R-POST",
        "LEAV",
        item_shown_at=f"{day}T10:00:00",
        decided_at=f"{day}T10:01:00",
    )
    return "CAMP-1:GL-E1:R-POST"


ACTIONS = {
    "je_prepare": (_act_je_prepare, "T10:00:00"),
    "je_approve": (_act_je_approve, "T11:00:00"),
    "prod_deploy": (_act_prod_deploy_manual, "T20:00:00"),
    "prod_deploy_pipeline": (_act_prod_deploy_pipeline, "T20:00:00"),
    "commit": (_act_commit, "T09:00:00"),
    "change_approval": (_act_change_approval, "T10:00:00"),
    "recert_decision": (_act_recert, "T10:01:00"),
}


@pytest.mark.parametrize("kind", sorted(ACTIONS))
def test_x03_each_action_type_after_exit(tmp_path, kind) -> None:
    act, time = ACTIONS[kind]
    world = gl_world()
    action_id = act(world, "2025-11-18")

    [f] = _run(world, tmp_path, "X-03")

    assert f.entity == "LEAV"
    assert f.severity == Severity.CRITICAL
    assert f.details["exit_date"] == LEAVER_EXIT
    assert f.details["actions"] == [
        {
            "type": kind.removesuffix("_pipeline"),
            "id": action_id,
            "ts": f"2025-11-18{time}",
        }
    ]
    assert f.details["days_after_exit_max"] == 3
    assert f.details["related"] == []


@pytest.mark.parametrize("kind", sorted(ACTIONS))
def test_x03_near_miss_action_on_the_exit_date(tmp_path, kind) -> None:
    world = gl_world()
    ACTIONS[kind][0](world, LEAVER_EXIT)

    assert _run(world, tmp_path, "X-03") == []


@pytest.mark.parametrize("kind", sorted(ACTIONS))
def test_x03_near_miss_action_before_the_exit_date(tmp_path, kind) -> None:
    world = gl_world()
    ACTIONS[kind][0](world, "2025-11-14")

    assert _run(world, tmp_path, "X-03") == []


def test_x03_day_after_exit_is_flagged(tmp_path) -> None:
    world = gl_world()
    _act_commit(world, "2025-11-16")

    [f] = _run(world, tmp_path, "X-03")

    assert f.details["days_after_exit_max"] == 1


def test_x03_one_finding_per_employee_with_sorted_actions(tmp_path) -> None:
    world = gl_world()
    _act_commit(world, "2025-12-01")
    _act_je_prepare(world, "2025-11-20")
    _act_recert(world, "2025-11-16")

    [f] = _run(world, tmp_path, "X-03")

    assert [(a["type"], a["ts"]) for a in f.details["actions"]] == [
        ("recert_decision", "2025-11-16T10:01:00"),
        ("je_prepare", "2025-11-20T10:00:00"),
        ("commit", "2025-12-01T09:00:00"),
    ]
    assert f.details["days_after_exit_max"] == 16


def test_x03_counts_only_actions_after_exit(tmp_path) -> None:
    world = gl_world()
    world.changes.commit(
        world.changes.change(author=None),
        author="LEAV",
        committed_ts="2025-11-01T09:00:00",
        sha="b" * 40,
    )  # before the exit
    _act_commit(world, "2025-11-20")

    [f] = _run(world, tmp_path, "X-03")

    assert [a["id"] for a in f.details["actions"]] == ["a" * 40]


def test_x03_second_account_counts_for_the_employee(tmp_path) -> None:
    world = gl_world()
    _act_je_prepare(world, "2025-11-20")  # GL-LEAV
    _act_prod_deploy_manual(world, "2025-11-21")  # AD-LEAV

    [f] = _run(world, tmp_path, "X-03")

    assert [a["type"] for a in f.details["actions"]] == ["je_prepare", "prod_deploy"]


def test_x03_employee_without_exit_date_is_not_flagged(tmp_path) -> None:
    world = gl_world()
    world.changes.commit(
        world.changes.change(author=None),
        author="E1",
        committed_ts="2026-09-01T09:00:00",
        sha="c" * 40,
    )

    assert _run(world, tmp_path, "X-03") == []


def test_x03_automatic_entries_are_nobodys_action(tmp_path) -> None:
    world = gl_world()
    _entry(world, preparer="GL-BATCH", approver="", source="automatic", day="2026-01-05")

    assert _run(world, tmp_path, "X-03") == []


def test_x03_unknown_employee_in_commits_is_skipped(tmp_path) -> None:
    world = gl_world()
    world.changes.commit(
        world.changes.change(author=None),
        author="E999",
        committed_ts="2026-09-01T09:00:00",
        sha="d" * 40,
    )

    assert _run(world, tmp_path, "X-03") == []


# ===========================================================================
# details.related
# ===========================================================================


def test_related_default_is_empty_list(tmp_path) -> None:
    world = gl_world()
    _both_rights(world)
    _entry(world)

    [f] = _run(world, tmp_path, "X-01")

    assert f.details["related"] == []


def test_x01_related_a05_and_b08_of_the_same_objects(tmp_path) -> None:
    world = gl_world()
    _both_rights(world)
    doc = _entry(world, amount="150000.00")
    other = _entry(world, amount="150000.00", preparer="GL-E2", approver="")  # not an X-01 entry
    related = [
        _fake("B-08", doc),
        _fake("A-05", "E1:SOD-GL-01"),
        _fake("A-05", "E2:SOD-GL-01"),  # other employee
        _fake("B-08", other),  # entry not in the X-01 finding
        _fake("B-01", "E1"),  # not part of the X-01 definition
        _fake("A-01", "GL-E1"),  # not part of the X-01 definition
    ]

    [f] = _run(world, tmp_path, "X-01", related=related)

    assert f.details["related"] == [["A-05", "E1:SOD-GL-01"], ["B-08", doc]]


def test_x02_related_b_findings_of_the_entry_and_a_findings_of_the_account(tmp_path) -> None:
    world = gl_world()
    doc = _entry(world, preparer="GL-E1", approver="GL-E2", doc_no="MJ-2025-000123")
    related = [
        _fake("B-08", doc),
        _fake("B-10", doc),
        _fake("B-06", f"MJ-2025:dup:{doc}"),
        _fake("B-10", "MJ-2025-000999"),  # other entry
        _fake("B-01", "E1"),  # B finding, but not about this doc_no
        _fake("A-07", "GL-E1"),  # acting account
        _fake("A-04", "GL-E2:R-APPR"),  # account:role of the approving account
        _fake("A-01", "GL-E10"),  # prefix of the id, not the account
        _fake("A-01", "GL-E3"),  # not an acting account
        _fake("A-05", "E1:SOD-GL-01"),  # employee, not an account
    ]

    [f] = _run(world, tmp_path, "X-02", related=related)

    assert f.details["related"] == [
        ["A-04", "GL-E2:R-APPR"],
        ["A-07", "GL-E1"],
        ["B-06", f"MJ-2025:dup:{doc}"],
        ["B-08", doc],
        ["B-10", doc],
    ]


def test_x02_related_uses_only_the_accounts_of_the_flagged_actions(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")  # preparer is entitled
    _entry(world, preparer="GL-E1", approver="GL-E2")  # approver is not
    related = [_fake("A-01", "GL-E1"), _fake("A-01", "GL-E2")]

    [f] = _run(world, tmp_path, "X-02", related=related)

    assert f.details["actions"] == ["je_approve"]
    assert f.details["related"] == [["A-01", "GL-E2"]]


def test_x03_related_a01_of_the_leavers_accounts(tmp_path) -> None:
    world = gl_world()
    _act_commit(world, "2025-11-20")
    related = [
        _fake("A-01", "AD-LEAV"),
        _fake("A-01", "GL-LEAV"),
        _fake("A-01", "GL-E1"),  # other employee's account
        _fake("A-02", "AD-LEAV"),  # not A-01
    ]

    [f] = _run(world, tmp_path, "X-03", related=related)

    assert f.details["related"] == [["A-01", "AD-LEAV"], ["A-01", "GL-LEAV"]]


def test_related_is_sorted_and_unique(tmp_path) -> None:
    world = gl_world()
    _act_commit(world, "2025-11-20")
    related = [_fake("A-01", "GL-LEAV"), _fake("A-01", "AD-LEAV"), _fake("A-01", "GL-LEAV")]

    [f] = _run(world, tmp_path, "X-03", related=related)

    assert f.details["related"] == [["A-01", "AD-LEAV"], ["A-01", "GL-LEAV"]]


# ===========================================================================
# Common behaviour
# ===========================================================================


def _everything(world: World) -> None:
    _both_rights(world)
    _entry(world)  # X-01
    _entry(world, preparer="GL-E3", approver="GL-E2", day="2025-04-01")  # X-02
    _act_commit(world, "2025-11-20")  # X-03


def test_run_crosslink_rules_returns_all_three_sorted(tmp_path) -> None:
    world = gl_world()
    _everything(world)

    findings = _run(world, tmp_path)

    keys = [(f.rule_id, f.entity) for f in findings]
    assert keys == sorted(keys)
    assert {f.rule_id for f in findings} == {"X-01", "X-02", "X-03"}
    assert len(findings) == len(set(keys))


def test_clean_bank_has_no_findings(tmp_path) -> None:
    world = gl_world()
    world.access.assign("GL-E1", "R-POST", valid_from="2024-12-01")
    world.access.assign("GL-E2", "R-APPR", valid_from="2024-12-01")
    _entry(world, preparer="GL-E1", approver="GL-E2")
    _entry(world, preparer="GL-BATCH", approver="", source="automatic")

    assert _run(world, tmp_path) == []


def test_registry_and_rule_metadata() -> None:
    assert CROSSLINK_RULES.ids() == ["X-01", "X-02", "X-03"]
    assert CROSSLINK_RULES.get("X-01") is SodExercised
    assert CROSSLINK_RULES.get("X-02") is ActionWithoutEntitlement
    assert CROSSLINK_RULES.get("X-03") is ActivityAfterExit
    for cls in (SodExercised, ActionWithoutEntitlement, ActivityAfterExit):
        assert cls.version == "1.0.0"
        assert cls.title


def test_evidence_lists_the_hashes_of_the_files_each_rule_read(tmp_path) -> None:
    world = gl_world()
    _everything(world)
    loaded = world.load(tmp_path)
    findings = {
        f.rule_id: f for f in run_crosslink_rules(loaded.access, loaded.journal, loaded.change)
    }

    def hashes(data, *names):
        return {data.evidence[n].sha256 for n in names}

    access_rights = (
        "accounts",
        "account_roles",
        "roles",
        "role_hierarchy",
        "entitlements",
        "role_entitlements",
    )
    assert set(findings["X-01"].evidence) == (
        hashes(loaded.access, *access_rights)
        | hashes(loaded.journal, "journal_entries", "journal_lines", "approval_limits")
    )
    assert set(findings["X-02"].evidence) == (
        hashes(loaded.access, *access_rights) | hashes(loaded.journal, "journal_entries")
    )
    assert set(findings["X-03"].evidence) == (
        hashes(loaded.access, "hr_employees", "accounts", "recert_decisions")
        | hashes(loaded.journal, "journal_entries")
        | hashes(loaded.change, "commits", "deployments", "change_approvals")
    )
    for f in findings.values():
        assert len(f.evidence) == len(set(f.evidence))


def test_finding_ids_are_stable_between_runs(tmp_path) -> None:
    world = gl_world()
    _everything(world)
    loaded = world.load(tmp_path)

    first = run_crosslink_rules(loaded.access, loaded.journal, loaded.change)
    second = run_crosslink_rules(loaded.access, loaded.journal, loaded.change)

    assert [f.finding_id for f in first] == [f.finding_id for f in second]
    assert first == second


def test_rules_do_not_modify_their_input(tmp_path) -> None:
    world = gl_world()
    _everything(world)
    loaded = world.load(tmp_path)
    related = [_fake("A-05", "E1:SOD-GL-01"), _fake("A-01", "AD-LEAV")]
    related_before = list(related)

    def snapshot():
        frames = {}
        for prefix, data in (("a", loaded.access), ("j", loaded.journal), ("c", loaded.change)):
            for name, ev in data.evidence.items():
                frames[f"{prefix}:{name}"] = ev.frame.copy()
        frames["a:accounts*"] = loaded.access.accounts.copy()
        frames["a:assignments*"] = loaded.access.assignments.copy()
        frames["a:employees*"] = loaded.access.employees.copy()
        frames["a:decisions*"] = loaded.access.decisions.copy()
        frames["j:entries*"] = loaded.journal.entries.copy()
        frames["c:deployments*"] = loaded.change.deployments.copy()
        frames["c:commits*"] = loaded.change.commits.copy()
        return frames

    before = snapshot()
    run_crosslink_rules(loaded.access, loaded.journal, loaded.change, related)
    after = snapshot()

    assert before.keys() == after.keys()
    for key in before:
        assert before[key].equals(after[key]), key
    assert related == related_before


def test_findings_carry_the_audit_date_in_their_params(tmp_path) -> None:
    world = gl_world()
    _everything(world)

    for f in _run(world, tmp_path):
        assert f.params["as_of"] == date(2026, 9, 30).isoformat()
