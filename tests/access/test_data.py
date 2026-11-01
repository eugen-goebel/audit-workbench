"""Tests for loading access data and the role graph."""

from datetime import date

import pytest

from audit_workbench.core.evidence import EvidenceError
from tests.access.builders import MiniBank


def test_active_assignments_respect_validity_window(tmp_path):
    bank = MiniBank(as_of=date(2026, 9, 30))
    bank.role("R1")
    bank.assign("A1", "R1", valid_from="2026-01-01", valid_to="")  # open-ended
    bank.assign("A2", "R1", valid_from="2026-01-01", valid_to="2026-09-30")  # ends on as_of
    bank.assign("A3", "R1", valid_from="2026-01-01", valid_to="2026-09-29")  # expired
    bank.assign("A4", "R1", valid_from="2026-10-01")  # not yet valid
    data = bank.load(tmp_path)
    assert sorted(data.active_assignments["account_id"]) == ["A1", "A2"]


def test_has_left_is_relative_to_as_of(tmp_path):
    bank = MiniBank(as_of=date(2026, 9, 30))
    bank.employee("E1", exit_date="2026-09-29")
    bank.employee("E2", exit_date="2026-09-30")  # last working day is the audit date
    bank.employee("E3")
    data = bank.load(tmp_path)
    assert data.employees["has_left"].to_dict() == {"E1": True, "E2": False, "E3": False}


def test_roles_within_follows_three_levels(tmp_path):
    bank = MiniBank()
    for r in ["R1", "R2", "R3", "R4"]:
        bank.role(r)
    bank.contains("R1", "R2").contains("R2", "R3").contains("R3", "R4")
    data = bank.load(tmp_path)
    assert data.roles_within("R1") == {"R1", "R2", "R3", "R4"}
    assert data.roles_within("R3") == {"R3", "R4"}


def test_privileged_through_inheritance(tmp_path):
    bank = MiniBank()
    bank.role("R-TEAM").role("R-MID").role("R-ADMIN", privileged=True)
    bank.contains("R-TEAM", "R-MID").contains("R-MID", "R-ADMIN")
    data = bank.load(tmp_path)
    assert data.is_privileged("R-TEAM")
    assert data.is_privileged("R-ADMIN")
    assert data.is_privileged("R-MID")  # R-MID contains R-ADMIN


def test_role_cycle_is_reported(tmp_path):
    bank = MiniBank()
    bank.role("R1").role("R2")
    bank.contains("R1", "R2").contains("R2", "R1")
    data = bank.load(tmp_path)
    with pytest.raises(EvidenceError, match="cycles"):
        _ = data.role_graph


def test_bad_boolean_is_rejected(tmp_path):
    bank = MiniBank()
    bank.account("A1", employee_id="E1")
    bank.rows["accounts"][0]["interactive"] = "yes"
    data = bank.load(tmp_path)
    with pytest.raises(EvidenceError, match="true/false"):
        _ = data.accounts


def test_hashes_returns_sha256_per_file(tmp_path):
    data = MiniBank().load(tmp_path)
    (h,) = data.hashes("accounts")
    assert len(h) == 64
