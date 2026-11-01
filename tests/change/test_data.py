"""Loading and derived views of the change management data."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from audit_workbench.core.evidence import EvidenceError

from .builders import MiniChanges


def test_period_is_twelve_months_up_to_as_of(tmp_path):
    data = MiniChanges().load(tmp_path)
    assert data.as_of == date(2026, 9, 30)
    assert data.period_start == date(2025, 10, 1)


def test_population_is_changes_with_prod_deployment_in_period(tmp_path):
    m = MiniChanges()
    deployed = m.change()
    m.deploy(deployed)
    only_test = m.change()
    m.deploy(only_test, environment="test")
    before = m.change(created_ts="2025-09-01T09:00:00")
    m.deploy(before, deployed_ts="2025-09-30T20:00:00")
    after = m.change()
    m.deploy(after, deployed_ts="2026-10-01T08:00:00")
    m.change()  # never deployed
    data = m.load(tmp_path)
    assert data.population == [deployed]


def test_unknown_change_id_is_kept_as_deployment_but_not_in_population(tmp_path):
    m = MiniChanges()
    dep = m.deploy("CHG-99999")
    data = m.load(tmp_path)
    assert data.population == []
    assert [d.deployment_id for d in data.prod_deployments] == [dep]


def test_deploying_employee_manual_and_pipeline(tmp_path):
    m = MiniChanges()
    cid = m.change()
    manual = m.deploy(cid, method="manual", account_id="AD-DEV2")
    pipe = m.deploy(cid, method="pipeline", released_by="DEV1", deployed_ts="2026-03-03T20:00:00")
    data = m.load(tmp_path)
    by_id = {d.deployment_id: d for d in data.prod_deployments}
    assert by_id[manual].deploying_employee_id == "DEV2"
    assert by_id[pipe].deploying_employee_id == "DEV1"
    assert by_id[pipe].account_id == "AD-PIPE"


def test_first_prod_deployment_counts_failed_attempts(tmp_path):
    m = MiniChanges()
    cid = m.change()
    failed = m.deploy(cid, deployed_ts="2026-03-02T20:00:00", result="failed")
    m.deploy(cid, deployed_ts="2026-03-03T20:00:00")
    data = m.load(tmp_path)
    assert data.first_prod_deployment(cid).deployment_id == failed


def test_effective_approval_rejection_then_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decided_ts="2026-03-01T10:00:00", decision="rejected")
    m.approval(cid, decided_ts="2026-03-01T12:00:00")
    data = m.load(tmp_path)
    assert data.effective_approval(cid).decided_ts == datetime(2026, 3, 1, 12, 0)


def test_effective_approval_approval_then_rejection_is_none(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decided_ts="2026-03-01T10:00:00")
    m.approval(cid, decided_ts="2026-03-01T12:00:00", decision="rejected")
    data = m.load(tmp_path)
    assert data.effective_approval(cid) is None


def test_post_approval_is_not_a_pre_approval(tmp_path):
    m = MiniChanges()
    cid = m.change(change_type="emergency", approver=None)
    m.approval(cid, stage="post", decided_ts="2026-03-04T10:00:00")
    data = m.load(tmp_path)
    assert data.effective_approval(cid) is None
    assert data.first_post_approval(cid).decided_ts == datetime(2026, 3, 4, 10, 0)


def test_commit_authors_and_shas(tmp_path):
    m = MiniChanges()
    cid = m.change(author="DEV1")
    sha2 = m.commit(cid, author="DEV2", sha="b" * 40)
    m.commit("", author="DEV2")  # commit without change
    data = m.load(tmp_path)
    assert data.commit_authors[cid] == frozenset({"DEV1", "DEV2"})
    assert data.commits_of(cid, author="DEV2") == [sha2]
    assert "" not in data.commit_authors


def test_primary_ad_account_is_the_oldest(tmp_path):
    m = MiniChanges()
    m.account("AD-DEV1-ADM", "DEV1", created_at="2024-05-01T08:00:00")
    data = m.load(tmp_path)
    assert data.primary_ad_account["DEV1"] == "AD-DEV1"


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("changes", "change_type", "urgent"),
        ("changes", "risk", "extreme"),
        ("deployments", "environment", "staging"),
        ("deployments", "method", "ftp"),
        ("change_approvals", "decision", "maybe"),
    ],
)
def test_unknown_values_are_data_errors(tmp_path, table, column, value):
    m = MiniChanges()
    cid = m.change()
    m.deploy(cid)
    m.rows[table][-1][column] = value
    with pytest.raises(EvidenceError, match=f"{table}.{column}"):
        m.load(tmp_path)


def test_unknown_account_is_a_data_error(tmp_path):
    m = MiniChanges()
    m.deploy(m.change(), account_id="AD-NOBODY")
    with pytest.raises(EvidenceError, match="unknown account"):
        m.load(tmp_path)


def test_manual_deployment_with_technical_account_is_a_data_error(tmp_path):
    m = MiniChanges()
    m.deploy(m.change(), method="manual", account_id="AD-PIPE")
    with pytest.raises(EvidenceError, match="non-personal"):
        m.load(tmp_path)


def test_pipeline_without_release_is_a_data_error(tmp_path):
    m = MiniChanges()
    m.deploy(m.change(), method="pipeline", released_by="")
    with pytest.raises(EvidenceError, match="released_by"):
        m.load(tmp_path)


def test_duplicate_change_id_is_a_data_error(tmp_path):
    m = MiniChanges()
    m.change("CHG-1")
    m.change("CHG-1")
    with pytest.raises(EvidenceError, match="duplicate"):
        m.load(tmp_path)


def test_invalid_timestamp_is_a_data_error(tmp_path):
    m = MiniChanges()
    m.deploy(m.change(), deployed_ts="2026-03-02 20:00")
    with pytest.raises(EvidenceError, match="deployments.deployed_ts"):
        m.load(tmp_path)


def test_approval_at_a_point_in_time(tmp_path):
    m = MiniChanges()
    cid = m.change(approver=None)
    m.approval(cid, decided_ts="2026-03-01T10:00:00")
    m.approval(cid, decided_ts="2026-03-01T12:00:00", decision="rejected")
    data = m.load(tmp_path)
    assert data.approval_at(cid, datetime(2026, 3, 1, 9, 0)) is None
    assert data.approval_at(cid, datetime(2026, 3, 1, 10, 0)) is not None
    assert data.approval_at(cid, datetime(2026, 3, 1, 12, 0)) is None
