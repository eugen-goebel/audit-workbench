"""Tests for change management rules C-01 to C-06 (spec docs/design/module-c-change.md, section 3).

Each rule starts with the positive cases and then the near misses of spec
section 4, so a change that makes a rule too eager or too lazy fails here.

Near misses that must NOT be flagged (spec section 4):
  C-01  standard change without approval; rejected, then approved before deploy
  C-02  deploy to test; developer of change X deploys unrelated change Y;
        pipeline released by a colleague of the author
  C-03  post-approval exactly 7 days after deployment; deployed 3 days before
        as_of with post-approval still open
  C-04  rejected then approved before deploy; approval and deployment at the same timestamp
  C-06  low-risk normal change without test sign-off
  all   failed deployment followed by a successful redeployment: once per rule
"""

from __future__ import annotations

import pytest

from audit_workbench.change.rules import (
    CHANGE_RULES,
    ApprovalAfterDeploy,
    ApproverNotIndependent,
    DeployWithoutChange,
    EmergencyPostApproval,
    SelfDeploy,
    UntestedChange,
    run_change_rules,
)
from audit_workbench.core.findings import Severity
from tests.change.builders import MiniChanges


def _entities(findings):
    return [f.entity for f in findings]


def _clean(m: MiniChanges) -> str:
    """A normal, approved, tested change deployed by the operator after approval."""
    cid = m.change()
    m.deploy(cid)
    return cid


# ===========================================================================
# C-01  deploy_without_change
# ===========================================================================


def test_c01_missing_change_id(tmp_path):
    m = MiniChanges()
    dep = m.deploy("")
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.rule_version, f.entity, f.severity) == (
        "C-01",
        "1.0.0",
        dep,
        Severity.HIGH,
    )
    assert f.details == {
        "triggers": ["missing"],
        "deploying_employee_id": "OPS1",
        "change_id": None,
    }


def test_c01_unknown_change_id(tmp_path):
    m = MiniChanges()
    dep = m.deploy("CHG-99999")
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["triggers"] == ["unknown"]
    assert findings[0].details["change_id"] == "CHG-99999"


def test_c01_system_mismatch(tmp_path):
    m = MiniChanges()
    cid = m.change(system="GL")
    dep = m.deploy(cid, system="PAY")
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["triggers"] == ["system_mismatch"]
    assert findings[0].details["change_id"] == cid


def test_c01_normal_change_without_any_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    dep = m.deploy(cid)
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["triggers"] == ["not_approved"]


def test_c01_normal_change_only_rejected(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decision="rejected")
    dep = m.deploy(cid)
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["triggers"] == ["not_approved"]


def test_c01_normal_change_approved_then_rejected(tmp_path):
    m = MiniChanges()
    cid = m.change(approved_ts="2026-03-01T10:00:00")
    m.approval(cid, decision="rejected", decided_ts="2026-03-01T12:00:00")
    dep = m.deploy(cid)
    assert _entities(DeployWithoutChange().run(m.load(tmp_path))) == [dep]


def test_c01_cancelled_change_even_if_approved(tmp_path):
    m = MiniChanges()
    cid = m.change(status="cancelled")
    dep = m.deploy(cid)
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["triggers"] == ["not_approved"]


def test_c01_cancelled_standard_change(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="standard", approver=None, status="cancelled")
    dep = m.deploy(cid)
    findings = DeployWithoutChange().run(m.load(tmp_path))
    assert _entities(findings) == [dep]
    assert findings[0].details["triggers"] == ["not_approved"]


def test_c01_triggers_combine_into_one_finding_sorted(tmp_path):
    m = MiniChanges()
    cid = m.change(system="GL", status="cancelled")
    dep = m.deploy(cid, system="PAY")
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["triggers"] == ["not_approved", "system_mismatch"]


def test_c01_pipeline_reports_the_releasing_employee(tmp_path):
    m = MiniChanges()
    dep = m.deploy("", method="pipeline", released_by="REL1")
    findings = DeployWithoutChange().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["deploying_employee_id"] == "REL1"


def test_c01_every_deployment_is_its_own_finding(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    d1 = m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    d2 = m.deploy(cid, deployed_ts="2026-03-03T20:00:00")
    findings = DeployWithoutChange().run(m.load(tmp_path))
    assert _entities(findings) == sorted([d1, d2])


def test_c01_near_miss_standard_change_without_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="standard", approver=None, risk="low")
    m.deploy(cid)
    assert DeployWithoutChange().run(m.load(tmp_path)) == []


def test_c01_near_miss_rejected_then_approved_before_deploy(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decision="rejected", decided_ts="2026-03-01T10:00:00")
    m.approval(cid, decision="approved", decided_ts="2026-03-01T14:00:00")
    m.deploy(cid)
    assert DeployWithoutChange().run(m.load(tmp_path)) == []


def test_c01_near_miss_emergency_without_pre_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="emergency", approver=None)
    m.deploy(cid)
    assert DeployWithoutChange().run(m.load(tmp_path)) == []


def test_c01_near_miss_approval_after_deploy_is_c04_not_c01(tmp_path):
    m = MiniChanges()
    cid = m.change(approved_ts="2026-03-03T08:00:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    assert DeployWithoutChange().run(m.load(tmp_path)) == []


def test_c01_near_miss_failed_then_successful_redeploy_of_clean_change(tmp_path):
    m = MiniChanges()
    cid = m.change()
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    m.deploy(cid, deployed_ts="2026-03-03T20:00:00")
    assert DeployWithoutChange().run(m.load(tmp_path)) == []


def test_c01_ignores_test_deployments_and_other_periods(tmp_path):
    m = MiniChanges()
    m.deploy("", environment="test")
    m.deploy("", deployed_ts="2025-09-30T23:59:59")  # day before the period
    m.deploy("", deployed_ts="2026-10-01T00:00:00")  # day after as_of
    assert DeployWithoutChange().run(m.load(tmp_path)) == []


def test_c01_period_boundaries_are_included(tmp_path):
    m = MiniChanges()
    d1 = m.deploy("", deployed_ts="2025-10-01T00:00:00")
    d2 = m.deploy("", deployed_ts="2026-09-30T23:59:59")
    assert _entities(DeployWithoutChange().run(m.load(tmp_path))) == sorted([d1, d2])


# ===========================================================================
# C-02  self_deploy
# ===========================================================================


def test_c02_developer_deploys_own_change_manually(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV1")
    sha = m.rows["commits"][-1]["commit_sha"]
    dep = m.deploy(cid, account_id="AD-DEV1")
    findings = SelfDeploy().run(m.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.rule_version, f.entity, f.severity) == (
        "C-02",
        "1.0.0",
        dep,
        Severity.HIGH,
    )
    assert f.details == {
        "deploying_employee_id": "DEV1",
        "account_id": "AD-DEV1",
        "method": "manual",
        "commit_shas": [sha],
        "via_second_account": False,
    }


def test_c02_via_second_account(tmp_path):
    m = MiniChanges()
    m.account("AD-DEV1-X", "DEV1", created_at="2025-06-01T08:00:00")
    cid = m.change(author="DEV1")
    dep = m.deploy(cid, account_id="AD-DEV1-X")
    findings = SelfDeploy().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    assert findings[0].details["deploying_employee_id"] == "DEV1"
    assert findings[0].details["via_second_account"] is True


def test_c02_pipeline_released_by_the_author(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV1")
    dep = m.deploy(cid, method="pipeline", released_by="DEV1")
    findings = SelfDeploy().run(m.load(tmp_path))

    assert _entities(findings) == [dep]
    d = findings[0].details
    assert d["deploying_employee_id"] == "DEV1"
    assert d["account_id"] == "AD-PIPE"
    assert d["method"] == "pipeline"
    assert d["via_second_account"] is False


def test_c02_commit_shas_are_the_deployers_commits_sorted(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV1")
    m.rows["commits"][-1]["commit_sha"] = "c" * 40
    m.commit(cid, author="DEV1", sha="a" * 40)
    m.commit(cid, author="DEV2", sha="b" * 40)
    m.deploy(cid, account_id="AD-DEV1")
    findings = SelfDeploy().run(m.load(tmp_path))

    assert findings[0].details["commit_shas"] == ["a" * 40, "c" * 40]


def test_c02_deployer_is_one_of_several_authors(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV2")
    m.commit(cid, author="DEV1")
    dep = m.deploy(cid, account_id="AD-DEV1")
    assert _entities(SelfDeploy().run(m.load(tmp_path))) == [dep]


def test_c02_every_deployment_of_the_change_is_flagged(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV1")
    d1 = m.deploy(cid, account_id="AD-DEV1", deployed_ts="2026-03-02T20:00:00", result="failed")
    d2 = m.deploy(cid, account_id="AD-DEV1", deployed_ts="2026-03-03T20:00:00")
    assert _entities(SelfDeploy().run(m.load(tmp_path))) == sorted([d1, d2])


def test_c02_near_miss_deploy_to_test(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV1")
    m.deploy(cid, account_id="AD-DEV1", environment="test")
    assert SelfDeploy().run(m.load(tmp_path)) == []


def test_c02_near_miss_developer_deploys_unrelated_change(tmp_path):
    m = MiniChanges()
    m.change("CHG-X", author="DEV1")  # DEV1 wrote X, X is not deployed
    y = m.change("CHG-Y", author="DEV2")
    m.deploy(y, account_id="AD-DEV1")
    assert SelfDeploy().run(m.load(tmp_path)) == []


def test_c02_near_miss_pipeline_released_by_colleague(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV1")
    m.deploy(cid, method="pipeline", released_by="DEV2")
    assert SelfDeploy().run(m.load(tmp_path)) == []


def test_c02_near_miss_operator_deploys(tmp_path):
    m = MiniChanges()
    _clean(m)
    assert SelfDeploy().run(m.load(tmp_path)) == []


def test_c02_deployment_without_change_has_no_commits(tmp_path):
    m = MiniChanges()
    m.commit("", author="DEV1")
    m.deploy("", account_id="AD-DEV1")
    assert SelfDeploy().run(m.load(tmp_path)) == []


# ===========================================================================
# C-03  emergency_post_approval
# ===========================================================================


def _emergency(m: MiniChanges, *, deployed: str, post: str | None, **kw) -> str:
    cid = m.change(change_type="emergency", approver=None, **kw)
    m.deploy(cid, deployed_ts=deployed)
    if post is not None:
        m.approval(cid, stage="post", decided_ts=post)
    return cid


def test_c03_missing_post_approval_is_high(tmp_path):
    m = MiniChanges()
    cid = _emergency(m, deployed="2026-03-02T20:00:00", post=None)
    findings = EmergencyPostApproval().run(m.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.rule_version, f.entity, f.severity) == (
        "C-03",
        "1.0.0",
        cid,
        Severity.HIGH,
    )
    assert f.params["post_days"] == 7
    assert f.details["trigger"] == "missing"
    assert f.details["post_days"] == 7
    assert f.details["first_deployment_id"] == "DEP-00001"
    assert "7" in f.description


def test_c03_only_rejected_post_decision_is_missing(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="emergency", approver=None)
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    m.approval(cid, stage="post", decision="rejected", decided_ts="2026-03-03T09:00:00")
    findings = EmergencyPostApproval().run(m.load(tmp_path))
    assert [(f.entity, f.details["trigger"]) for f in findings] == [(cid, "missing")]


def test_c03_pre_approval_does_not_replace_post_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="emergency")  # default pre approval by CAB1
    m.deploy(cid)
    assert _entities(EmergencyPostApproval().run(m.load(tmp_path))) == [cid]


def test_c03_late_post_approval_on_day_8_is_medium(tmp_path):
    m = MiniChanges()
    cid = _emergency(m, deployed="2026-03-02T20:00:00", post="2026-03-10T09:00:00")
    findings = EmergencyPostApproval().run(m.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.entity, f.severity) == (cid, Severity.MEDIUM)
    assert f.details["trigger"] == "late"
    assert f.details["days_after_deployment"] == 8
    assert f.details["approved_ts"] == "2026-03-10T09:00:00"
    assert f.details["deployed_ts"] == "2026-03-02T20:00:00"


def test_c03_near_miss_post_approved_exactly_7_days_after(tmp_path):
    m = MiniChanges()
    _emergency(m, deployed="2026-03-02T20:00:00", post="2026-03-09T20:00:00")
    assert EmergencyPostApproval().run(m.load(tmp_path)) == []


def test_c03_calendar_days_not_hours(tmp_path):
    # 7 calendar days later, but 7 days and 13 hours after the deployment: still day 7
    m = MiniChanges()
    _emergency(m, deployed="2026-03-02T08:00:00", post="2026-03-09T21:00:00")
    assert EmergencyPostApproval().run(m.load(tmp_path)) == []


def test_c03_first_approved_post_decision_counts(tmp_path):
    m = MiniChanges()
    cid = _emergency(m, deployed="2026-03-02T20:00:00", post="2026-03-10T09:00:00")
    m.approval(cid, stage="post", decided_ts="2026-03-03T09:00:00")  # earlier, in time
    assert EmergencyPostApproval().run(m.load(tmp_path)) == []


def test_c03_near_miss_deployed_3_days_before_as_of_still_open(tmp_path):
    m = MiniChanges()
    _emergency(m, deployed="2026-09-27T10:00:00", post=None)
    assert EmergencyPostApproval().run(m.load(tmp_path)) == []


def test_c03_open_approval_flagged_once_post_days_have_passed(tmp_path):
    m = MiniChanges()
    exactly_7 = _emergency(m, deployed="2026-09-23T10:00:00", post=None)  # 7 days: not yet
    day_8 = _emergency(m, deployed="2026-09-22T10:00:00", post=None)  # 8 days: flagged
    findings = EmergencyPostApproval().run(m.load(tmp_path))
    assert _entities(findings) == [day_8]
    assert exactly_7 not in _entities(findings)
    assert findings[0].details["trigger"] == "missing"


def test_c03_post_days_parameter(tmp_path):
    m = MiniChanges()
    cid = _emergency(m, deployed="2026-03-02T20:00:00", post="2026-03-06T09:00:00")  # day 4
    data = m.load(tmp_path)
    assert EmergencyPostApproval().run(data) == []
    findings = EmergencyPostApproval(post_days=3).run(data)
    assert _entities(findings) == [cid]
    assert findings[0].params["post_days"] == 3
    assert findings[0].details["post_days"] == 3


def test_c03_measures_from_the_first_failed_deployment_and_counts_once(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="emergency", approver=None)
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    m.deploy(cid, deployed_ts="2026-03-08T20:00:00")
    m.approval(cid, stage="post", decided_ts="2026-03-10T09:00:00")  # day 8 from the failed one
    findings = EmergencyPostApproval().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    assert findings[0].details["first_deployment_id"] == "DEP-00001"


def test_c03_ignores_other_change_types_and_changes_without_prod_deployment(tmp_path):
    m = MiniChanges()
    n = m.change(change_type="normal")
    m.deploy(n)
    s = m.change(change_type="standard", approver=None)
    m.deploy(s)
    e = m.change(change_type="emergency", approver=None)
    m.deploy(e, environment="test")
    e2 = m.change(change_type="emergency", approver=None)
    m.deploy(e2, deployed_ts="2025-09-01T10:00:00")  # before the period
    assert EmergencyPostApproval().run(m.load(tmp_path)) == []


# ===========================================================================
# C-04  approval_after_deploy
# ===========================================================================


def test_c04_deployed_before_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(approved_ts="2026-03-03T08:00:00")
    dep = m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    findings = ApprovalAfterDeploy().run(m.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.rule_version, f.entity, f.severity) == (
        "C-04",
        "1.0.0",
        cid,
        Severity.HIGH,
    )
    assert f.details == {
        "first_deployment_id": dep,
        "deployed_ts": "2026-03-02T20:00:00",
        "approved_ts": "2026-03-03T08:00:00",
        "hours_before_approval": 12.0,
    }


def test_c04_hours_before_approval_is_fractional(tmp_path):
    m = MiniChanges()
    cid = m.change(approved_ts="2026-03-02T21:30:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    assert ApprovalAfterDeploy().run(m.load(tmp_path))[0].details["hours_before_approval"] == 1.5


def test_c04_failed_deployment_before_approval_counts(tmp_path):
    m = MiniChanges()
    cid = m.change(approved_ts="2026-03-03T08:00:00")
    d1 = m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    m.deploy(cid, deployed_ts="2026-03-03T20:00:00")  # after approval
    findings = ApprovalAfterDeploy().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    assert findings[0].details["first_deployment_id"] == d1


def test_c04_near_miss_reapproval_after_an_approved_deployment(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decided_ts="2026-03-01T10:00:00")  # in force at deployment
    m.approval(cid, decided_ts="2026-03-04T10:00:00")  # later re-approval, not late
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    assert ApprovalAfterDeploy().run(m.load(tmp_path)) == []


def test_c04_rejected_before_deploy_and_approved_after(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decided_ts="2026-03-01T10:00:00")
    m.approval(cid, decision="rejected", decided_ts="2026-03-01T12:00:00")
    m.approval(cid, decided_ts="2026-03-04T10:00:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    findings = ApprovalAfterDeploy().run(m.load(tmp_path))
    assert [f.entity for f in findings] == [cid]
    assert findings[0].details["approved_ts"] == "2026-03-04T10:00:00"


def test_c04_near_miss_equal_timestamps(tmp_path):
    m = MiniChanges()
    cid = m.change(approved_ts="2026-03-02T20:00:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    assert ApprovalAfterDeploy().run(m.load(tmp_path)) == []


def test_c04_near_miss_rejected_then_approved_before_deploy(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decision="rejected", decided_ts="2026-03-01T10:00:00")
    m.approval(cid, decision="approved", decided_ts="2026-03-01T14:00:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    assert ApprovalAfterDeploy().run(m.load(tmp_path)) == []


def test_c04_near_miss_failed_then_successful_redeploy_after_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(approved_ts="2026-03-01T16:00:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    m.deploy(cid, deployed_ts="2026-03-03T20:00:00")
    assert ApprovalAfterDeploy().run(m.load(tmp_path)) == []


def test_c04_out_of_scope_changes(tmp_path):
    m = MiniChanges()
    for kind in ("standard", "emergency"):
        cid = m.change(change_type=kind, approved_ts="2026-03-03T08:00:00")
        m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    no_approval = m.change(approver=None)  # C-01 covers this one
    m.deploy(no_approval)
    post_only = m.change(approver=None)
    m.approval(post_only, stage="post", decided_ts="2026-03-05T08:00:00")
    m.deploy(post_only)
    assert ApprovalAfterDeploy().run(m.load(tmp_path)) == []


# ===========================================================================
# C-05  approver_not_independent
# ===========================================================================


def test_c05_approver_is_requester(tmp_path):
    m = MiniChanges()
    cid = m.change(approver="REQ1")
    m.deploy(cid)
    findings = ApproverNotIndependent().run(m.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.rule_version, f.entity, f.severity) == (
        "C-05",
        "1.0.0",
        cid,
        Severity.HIGH,
    )
    assert f.details == {
        "approver_employee_id": "REQ1",
        "approval_stage": "pre",
        "role": "requester",
    }


def test_c05_approver_is_commit_author(tmp_path):
    m = MiniChanges()
    cid = m.change(approver="DEV1", author="DEV1")
    m.deploy(cid)
    findings = ApproverNotIndependent().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    assert findings[0].details["role"] == "author"


def test_c05_approver_is_requester_and_author(tmp_path):
    m = MiniChanges()
    cid = m.change(approver="REQ1", requester="REQ1", author="REQ1")
    m.deploy(cid)
    assert ApproverNotIndependent().run(m.load(tmp_path))[0].details["role"] == "both"


def test_c05_post_approval_of_an_emergency_change(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="emergency", approver=None, author="DEV1")
    m.approval(cid, stage="post", approver="DEV1", decided_ts="2026-03-03T09:00:00")
    m.deploy(cid)
    findings = ApproverNotIndependent().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    assert findings[0].details["approval_stage"] == "post"
    assert findings[0].details["role"] == "author"


def test_c05_one_finding_per_change_with_several_affected_approvals(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None, author="DEV1")
    m.approval(cid, approver="DEV1", decided_ts="2026-03-01T10:00:00")
    m.approval(cid, approver="REQ1", decided_ts="2026-03-01T11:00:00")
    m.approval(cid, approver="CAB1", decided_ts="2026-03-01T12:00:00")  # independent
    m.approval(cid, approver="REQ1", stage="post", decided_ts="2026-03-05T12:00:00")
    m.deploy(cid)
    findings = ApproverNotIndependent().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    # the earliest affected approval is reported
    assert findings[0].details["approver_employee_id"] == "DEV1"
    assert findings[0].details["role"] == "author"


def test_c05_rejection_by_the_requester_is_not_an_approval(tmp_path):
    m = MiniChanges()
    cid = m.change()
    m.approval(cid, approver="REQ1", decision="rejected", decided_ts="2026-03-01T10:00:00")
    m.deploy(cid)
    assert ApproverNotIndependent().run(m.load(tmp_path)) == []


def test_c05_near_miss_independent_approver(tmp_path):
    m = MiniChanges()
    _clean(m)
    assert ApproverNotIndependent().run(m.load(tmp_path)) == []


def test_c05_authorship_of_another_change_does_not_count(tmp_path):
    m = MiniChanges()
    other = m.change("CHG-OTHER", author="CAB1")
    cid = m.change("CHG-MINE", approver="CAB1", author="DEV1")
    m.deploy(cid)
    assert other != cid
    assert ApproverNotIndependent().run(m.load(tmp_path)) == []


def test_c05_change_without_prod_deployment_in_period_is_out_of_population(tmp_path):
    m = MiniChanges()
    a = m.change(approver="REQ1")
    m.deploy(a, environment="test")
    b = m.change(approver="REQ1")
    m.deploy(b, deployed_ts="2025-09-30T10:00:00")
    m.change(approver="REQ1")  # never deployed
    assert ApproverNotIndependent().run(m.load(tmp_path)) == []


def test_c05_counted_once_for_failed_then_successful_redeploy(tmp_path):
    m = MiniChanges()
    cid = m.change(approver="REQ1")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    m.deploy(cid, deployed_ts="2026-03-03T20:00:00")
    assert _entities(ApproverNotIndependent().run(m.load(tmp_path))) == [cid]


# ===========================================================================
# C-06  untested_change
# ===========================================================================


def test_c06_missing_signoff(tmp_path):
    m = MiniChanges()
    cid = m.change(test_signoff_ts=None)
    m.deploy(cid)
    findings = UntestedChange().run(m.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.rule_version, f.entity, f.severity) == (
        "C-06",
        "1.0.0",
        cid,
        Severity.MEDIUM,
    )
    assert f.details == {"trigger": "missing"}


def test_c06_signoff_after_first_deployment(tmp_path):
    m = MiniChanges()
    cid = m.change(test_signoff_ts="2026-03-03T09:00:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    findings = UntestedChange().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    assert findings[0].details == {"trigger": "after_deploy"}


def test_c06_self_tested_by_a_commit_author(tmp_path):
    m = MiniChanges()
    cid = m.change(tester="DEV1", author="DEV1")
    m.deploy(cid)
    findings = UntestedChange().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    assert findings[0].details == {"trigger": "self_tested"}


def test_c06_high_risk_is_in_scope(tmp_path):
    m = MiniChanges()
    cid = m.change(risk="high", test_signoff_ts=None)
    m.deploy(cid)
    assert _entities(UntestedChange().run(m.load(tmp_path))) == [cid]


def test_c06_trigger_precedence_is_missing_after_deploy_self_tested(tmp_path):
    m = MiniChanges()
    both = m.change(tester="DEV1", author="DEV1", test_signoff_ts="2026-03-03T09:00:00")
    m.deploy(both, deployed_ts="2026-03-02T20:00:00")
    findings = UntestedChange().run(m.load(tmp_path))
    assert findings[0].details["trigger"] == "after_deploy"


def test_c06_near_miss_low_risk_without_signoff(tmp_path):
    m = MiniChanges()
    cid = m.change(risk="low", test_signoff_ts=None)
    m.deploy(cid)
    assert UntestedChange().run(m.load(tmp_path)) == []


def test_c06_standard_and_emergency_changes_are_out_of_scope(tmp_path):
    m = MiniChanges()
    for kind in ("standard", "emergency"):
        cid = m.change(change_type=kind, test_signoff_ts=None)
        m.deploy(cid)
    assert UntestedChange().run(m.load(tmp_path)) == []


def test_c06_near_miss_signoff_at_deployment_time(tmp_path):
    m = MiniChanges()
    cid = m.change(test_signoff_ts="2026-03-02T20:00:00")
    m.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    assert UntestedChange().run(m.load(tmp_path)) == []


def test_c06_near_miss_independent_tester_before_deploy(tmp_path):
    m = MiniChanges()
    _clean(m)
    assert UntestedChange().run(m.load(tmp_path)) == []


def test_c06_signoff_between_failed_and_successful_deployment_counts_once(tmp_path):
    m = MiniChanges()
    cid = m.change(test_signoff_ts="2026-03-03T09:00:00")
    d1 = m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    m.deploy(cid, deployed_ts="2026-03-03T20:00:00")
    findings = UntestedChange().run(m.load(tmp_path))

    assert _entities(findings) == [cid]
    assert findings[0].details["trigger"] == "after_deploy"
    assert d1  # the failed attempt is the reference point


def test_c06_change_without_prod_deployment_in_period_is_out_of_population(tmp_path):
    m = MiniChanges()
    a = m.change(test_signoff_ts=None)
    m.deploy(a, environment="test")
    m.change(test_signoff_ts=None)
    assert UntestedChange().run(m.load(tmp_path)) == []


# ===========================================================================
# Registry, evidence, determinism
# ===========================================================================


def test_registry_lists_the_six_rules():
    assert CHANGE_RULES.ids() == ["C-01", "C-02", "C-03", "C-04", "C-05", "C-06"]
    classes = [
        DeployWithoutChange,
        SelfDeploy,
        EmergencyPostApproval,
        ApprovalAfterDeploy,
        ApproverNotIndependent,
        UntestedChange,
    ]
    assert [CHANGE_RULES.get(c.rule_id) for c in classes] == classes
    assert all(c.version == "1.0.0" and c.title for c in classes)


def test_clean_data_gives_no_findings(tmp_path):
    m = MiniChanges()
    _clean(m)
    cid = m.change(change_type="standard", approver=None, risk="low", test_signoff_ts=None)
    m.deploy(cid, method="pipeline", released_by="REL1")
    assert run_change_rules(m.load(tmp_path)) == []


def test_run_change_rules_sorts_by_rule_and_entity(tmp_path):
    m = MiniChanges()
    a = m.deploy("", deployed_ts="2026-03-05T10:00:00", deployment_id="DEP-B")
    b = m.deploy("", deployed_ts="2026-03-04T10:00:00", deployment_id="DEP-A")
    c = m.change(approver="REQ1", test_signoff_ts=None)
    m.deploy(c)
    findings = run_change_rules(m.load(tmp_path))
    keys = [(f.rule_id, f.entity) for f in findings]

    assert keys == sorted(keys)
    assert {a, b} <= set(_entities(findings))
    assert {r for r, _ in keys} == {"C-01", "C-05", "C-06"}


def test_run_change_rules_params_and_unknown_rule(tmp_path):
    m = MiniChanges()
    _emergency(m, deployed="2026-03-02T20:00:00", post="2026-03-06T09:00:00")
    data = m.load(tmp_path)
    assert run_change_rules(data) == []
    assert [f.rule_id for f in run_change_rules(data, {"C-03": {"post_days": 3}})] == ["C-03"]
    with pytest.raises(KeyError, match="C-99"):
        run_change_rules(data, {"C-99": {}})


def test_findings_carry_evidence_hashes_params_and_references(tmp_path):
    m = MiniChanges()
    m.deploy("")
    cid = m.change(change_type="emergency", approver="REQ1", author="REQ1", risk="high")
    m.deploy(cid, account_id="AD-DEV1", deployed_ts="2026-03-02T20:00:00")
    m.rows["commits"][-1]["author_employee_id"] = "DEV1"
    data = m.load(tmp_path)
    findings = run_change_rules(data)
    assert {f.rule_id for f in findings} >= {"C-01", "C-02", "C-03", "C-05"}

    used = {
        "C-01": ("deployments", "changes", "change_approvals", "accounts"),
        "C-02": ("deployments", "commits", "accounts"),
        "C-03": ("changes", "change_approvals", "deployments"),
        "C-05": ("changes", "change_approvals", "commits", "deployments"),
    }
    for f in findings:
        assert f.params["as_of"] == "2026-09-30"
        assert f.regulation_refs
        if f.rule_id in used:
            assert f.evidence == data.hashes(*used[f.rule_id])


def test_finding_ids_are_stable_between_runs(tmp_path):
    m = MiniChanges()
    m.deploy("")
    cid = m.change(approver="REQ1", test_signoff_ts=None)
    m.deploy(cid)
    first = [f.finding_id for f in run_change_rules(m.load(tmp_path / "a"))]
    second = [f.finding_id for f in run_change_rules(m.load(tmp_path / "b"))]
    assert first == second and first
