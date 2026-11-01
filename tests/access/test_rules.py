"""Tests for access-review rules A-01 to A-08.

Tests are written test-first (TDD): each group starts with the positive case
and the near-misses called out in spec section 5.4.  Additional edge cases
cover the parameter defaults, entity formats and evidence hashing.

Near-misses that must NOT be flagged (from spec §5 point 4):
  A-01  leaver with disabled account; leaver inside grace_days
  A-02  last login exactly 89 days ago; non-interactive account; technical
        account never logged in; account whose employee has left (A-01 wins)
  A-03  non-interactive generic; disabled generic
  A-04  privileged role reviewed 300 days ago; non-privileged role
  A-05  employee with two non-conflicting functions; disabled account;
        expired assignment
  A-06  slow reviewer; reviewer with < min_items decisions; revoke on own
        account (not a keep)
  A-07  personal account with valid employee in HR
  A-08  rare role in a peer group below min_peers; role held by many peers
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from audit_workbench.access.rules import (
    ACCESS_RULES,
    DormantAccount,
    GenericInteractive,
    LeaverActive,
    OrphanAccount,
    PrivilegedUnreviewed,
    RecertRubberStamp,
    SodConflictRule,
    UnusualRole,
    run_access_rules,
)
from audit_workbench.access.sod import employee_functions, load_sod_ruleset
from audit_workbench.core.findings import Severity
from tests.access.builders import MiniBank

AS_OF = date(2026, 9, 30)


# ===========================================================================
# A-01  leaver_active
# ===========================================================================


def test_a01_flags_active_personal_account_after_exit(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)

    findings = LeaverActive().run(data)
    assert len(findings) == 1
    assert findings[0].entity == "PAY-1"
    assert findings[0].severity == Severity.HIGH
    assert findings[0].rule_id == "A-01"


def test_a01_entity_is_account_id(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("CORE-007", employee_id="E1")
    data = bank.load(tmp_path)
    findings = LeaverActive().run(data)
    assert findings[0].entity == "CORE-007"


def test_a01_details_keys(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1", last_login="2026-08-05T10:00:00")
    data = bank.load(tmp_path)
    f = LeaverActive().run(data)[0]
    assert f.details["employee_id"] == "E1"
    assert f.details["exit_date"] == "2026-08-01"
    assert f.details["days_since_exit"] == (AS_OF - date(2026, 8, 1)).days
    assert f.details["logged_in_after_exit"] is True


def test_a01_logged_in_before_exit(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1", last_login="2026-07-30T09:00:00")
    data = bank.load(tmp_path)
    f = LeaverActive().run(data)[0]
    assert f.details["logged_in_after_exit"] is False


def test_a01_nat_last_login(tmp_path):
    """Account never logged in — logged_in_after_exit must be False, not crash."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1", last_login="")
    data = bank.load(tmp_path)
    f = LeaverActive().run(data)[0]
    assert f.details["logged_in_after_exit"] is False
    assert f.details["last_login"] is None


def test_a01_near_miss_disabled_account_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1", status="disabled")
    data = bank.load(tmp_path)
    assert LeaverActive().run(data) == []


def test_a01_near_miss_leaver_inside_grace_days_not_flagged(tmp_path):
    """Employee left 5 days ago with grace_days=7 → not yet flagged."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date=str(AS_OF - timedelta(days=5)))
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)
    assert LeaverActive(grace_days=7).run(data) == []


def test_a01_near_miss_exactly_at_grace_boundary_not_flagged(tmp_path):
    """exit_date + grace_days == as_of  →  NOT flagged (condition is strict <)."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date=str(AS_OF - timedelta(days=7)))
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)
    assert LeaverActive(grace_days=7).run(data) == []


def test_a01_one_day_past_grace_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date=str(AS_OF - timedelta(days=8)))
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)
    assert len(LeaverActive(grace_days=7).run(data)) == 1


def test_a01_no_exit_date_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")  # still employed
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)
    assert LeaverActive().run(data) == []


def test_a01_employee_not_in_hr_not_flagged(tmp_path):
    """Account with unknown employee_id is A-07's problem, not A-01."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", employee_id="GHOST")
    data = bank.load(tmp_path)
    assert LeaverActive().run(data) == []


def test_a01_sorted_by_entity(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01").employee("E2", exit_date="2026-08-01")
    bank.account("PAY-Z", employee_id="E1").account("PAY-A", employee_id="E2")
    data = bank.load(tmp_path)
    findings = LeaverActive().run(data)
    assert [f.entity for f in findings] == ["PAY-A", "PAY-Z"]


def test_a01_params_include_as_of(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)
    f = LeaverActive().run(data)[0]
    assert f.params["as_of"] == AS_OF.isoformat()


# ===========================================================================
# A-02  dormant_account
# ===========================================================================


def test_a02_flags_dormant_account(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", last_login="2026-07-01T09:00:00")  # 91 days before AS_OF
    data = bank.load(tmp_path)
    findings = DormantAccount().run(data)
    assert any(f.entity == "PAY-1" for f in findings)
    assert all(f.severity == Severity.MEDIUM for f in findings)


def test_a02_near_miss_89_days_not_flagged(tmp_path):
    """89 days inactive with default dormant_days=90 → not dormant."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", last_login="2026-07-03T09:00:00")  # 89 days before AS_OF
    data = bank.load(tmp_path)
    assert DormantAccount().run(data) == []


def test_a02_near_miss_exactly_90_days_not_flagged(tmp_path):
    """login exactly at the cutoff (90 days before) is not older → not flagged."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", last_login="2026-07-02T00:00:00")  # exactly 90 days
    data = bank.load(tmp_path)
    assert DormantAccount().run(data) == []


def test_a02_near_miss_91_days_flagged(tmp_path):
    """91 days: crosses the threshold → flagged."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", last_login="2026-07-01T00:00:00")  # 91 days
    data = bank.load(tmp_path)
    findings = DormantAccount().run(data)
    assert any(f.entity == "PAY-1" for f in findings)


def test_a02_near_miss_non_interactive_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", interactive=False, last_login="2026-01-01T00:00:00")
    data = bank.load(tmp_path)
    assert DormantAccount().run(data) == []


def test_a02_near_miss_technical_never_logged_in_non_interactive_not_flagged(tmp_path):
    """Technical service account, interactive=False, never logged in → not flagged."""
    bank = MiniBank(as_of=AS_OF)
    bank.account(
        "SVC-1",
        account_type="technical",
        interactive=False,
        last_login="",
        created_at="2020-01-01T08:00:00",
    )
    data = bank.load(tmp_path)
    assert DormantAccount().run(data) == []


def test_a02_near_miss_leaver_excluded(tmp_path):
    """Leaver's account: A-01 wins; A-02 must not also flag it."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1", last_login="2026-01-01T00:00:00")
    data = bank.load(tmp_path)
    dormant = [f.entity for f in DormantAccount().run(data)]
    assert "PAY-1" not in dormant


def test_a02_never_logged_in_old_account_flagged(tmp_path):
    """last_login=NaT but account created >dormant_days ago → flagged."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", last_login="", created_at="2020-01-01T08:00:00")
    data = bank.load(tmp_path)
    findings = DormantAccount().run(data)
    assert any(f.entity == "PAY-1" for f in findings)


def test_a02_never_logged_in_new_account_not_flagged(tmp_path):
    """last_login=NaT, account just created → not flagged."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", last_login="", created_at="2026-09-29T08:00:00")
    data = bank.load(tmp_path)
    assert DormantAccount().run(data) == []


def test_a02_dormant_days_override(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    # last login 50 days ago — under default 90, but over custom 30
    bank.account("PAY-1", last_login=str(AS_OF - timedelta(days=50)) + "T00:00:00")
    data = bank.load(tmp_path)
    assert DormantAccount(dormant_days=30).run(data) != []
    assert DormantAccount(dormant_days=90).run(data) == []


# ===========================================================================
# A-03  generic_interactive
# ===========================================================================


def test_a03_flags_generic_interactive_active(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("GEN-1", account_type="generic", interactive=True, status="active")
    data = bank.load(tmp_path)
    findings = GenericInteractive().run(data)
    assert len(findings) == 1
    assert findings[0].entity == "GEN-1"
    assert findings[0].severity == Severity.MEDIUM


def test_a03_near_miss_non_interactive_generic_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("GEN-1", account_type="generic", interactive=False, status="active")
    data = bank.load(tmp_path)
    assert GenericInteractive().run(data) == []


def test_a03_near_miss_disabled_generic_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("GEN-1", account_type="generic", interactive=True, status="disabled")
    data = bank.load(tmp_path)
    assert GenericInteractive().run(data) == []


def test_a03_personal_interactive_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", account_type="personal", interactive=True, status="active")
    data = bank.load(tmp_path)
    assert GenericInteractive().run(data) == []


def test_a03_entity_format(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("GEN-ABC", account_type="generic")
    data = bank.load(tmp_path)
    assert GenericInteractive().run(data)[0].entity == "GEN-ABC"


# ===========================================================================
# A-04  privileged_unreviewed
# ===========================================================================


def _setup_privileged_account(bank: MiniBank, *, reviewed: bool, days_ago: int = 30) -> MiniBank:
    bank.employee("E1")
    bank.account("PAY-1", employee_id="E1", system="PAY")
    bank.role("R-PRIV", system="PAY", privileged=True)
    bank.assign("PAY-1", "R-PRIV")
    if reviewed:
        end = str(AS_OF - timedelta(days=days_ago))
        bank.campaign("C1", system="PAY", start_date="2026-01-01", end_date=end)
        bank.decision("C1", "PAY-1", "R-PRIV", "REV-1")
    return bank


def test_a04_flags_privileged_without_review(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    _setup_privileged_account(bank, reviewed=False)
    data = bank.load(tmp_path)
    findings = PrivilegedUnreviewed().run(data)
    assert len(findings) == 1
    assert findings[0].entity == "PAY-1:R-PRIV"
    assert findings[0].severity == Severity.HIGH


def test_a04_entity_format(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    _setup_privileged_account(bank, reviewed=False)
    data = bank.load(tmp_path)
    assert PrivilegedUnreviewed().run(data)[0].entity == "PAY-1:R-PRIV"


def test_a04_near_miss_reviewed_300_days_ago_not_flagged(tmp_path):
    """Review campaign ended 300 days ago — within default recert_days=365."""
    bank = MiniBank(as_of=AS_OF)
    _setup_privileged_account(bank, reviewed=True, days_ago=300)
    data = bank.load(tmp_path)
    assert PrivilegedUnreviewed().run(data) == []


def test_a04_reviewed_outside_window_flagged(tmp_path):
    """Campaign ended 366 days ago → outside recert_days=365 window → flagged."""
    bank = MiniBank(as_of=AS_OF)
    _setup_privileged_account(bank, reviewed=True, days_ago=366)
    data = bank.load(tmp_path)
    assert len(PrivilegedUnreviewed().run(data)) == 1


def test_a04_privileged_through_inheritance_flagged(tmp_path):
    """Role is not directly privileged but contains a privileged child — still flagged."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-1", employee_id="E1", system="PAY")
    bank.role("R-PARENT", system="PAY", privileged=False)
    bank.role("R-CHILD", system="PAY", privileged=True)
    bank.contains("R-PARENT", "R-CHILD")
    bank.assign("PAY-1", "R-PARENT")
    data = bank.load(tmp_path)
    findings = PrivilegedUnreviewed().run(data)
    assert any(f.entity == "PAY-1:R-PARENT" for f in findings)


def test_a04_near_miss_non_privileged_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-1", employee_id="E1")
    bank.role("R-BASIC", privileged=False)
    bank.assign("PAY-1", "R-BASIC")
    data = bank.load(tmp_path)
    assert PrivilegedUnreviewed().run(data) == []


def test_a04_near_miss_disabled_account_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-1", employee_id="E1", status="disabled")
    bank.role("R-PRIV", privileged=True)
    bank.assign("PAY-1", "R-PRIV")
    data = bank.load(tmp_path)
    assert PrivilegedUnreviewed().run(data) == []


def test_a04_campaign_wrong_system_not_counted(tmp_path):
    """Campaign on GL system doesn't certify a PAY account's role."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-1", employee_id="E1", system="PAY")
    bank.role("R-PRIV", system="PAY", privileged=True)
    bank.assign("PAY-1", "R-PRIV")
    bank.campaign("C1", system="GL", start_date="2026-01-01", end_date="2026-03-31")
    bank.decision("C1", "PAY-1", "R-PRIV", "REV-1")
    data = bank.load(tmp_path)
    # Campaign system (GL) ≠ account system (PAY) → review doesn't count
    assert len(PrivilegedUnreviewed().run(data)) == 1


def test_a04_unknown_role_in_assignment_does_not_crash(tmp_path):
    """Assignment referencing a role_id not in roles table → silently skip."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-1", employee_id="E1")
    # Deliberately no bank.role(...) for "R-GHOST"
    bank.rows["account_roles"].append(
        dict(account_id="PAY-1", role_id="R-GHOST", valid_from="2020-01-01", valid_to="")
    )
    data = bank.load(tmp_path)
    assert PrivilegedUnreviewed().run(data) == []  # unknown role → not privileged


# ===========================================================================
# A-05  sod_conflict
# ===========================================================================


def _sod_bank_simple(bank: MiniBank) -> MiniBank:
    """Minimal bank where E1 holds PAYMENT_CREATE and PAYMENT_APPROVE."""
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1")
    bank.role("R-CREATE").role("R-APPROVE")
    bank.entitlement("E-CREATE", "PAYMENT_CREATE")
    bank.entitlement("E-APPROVE", "PAYMENT_APPROVE")
    bank.grants("R-CREATE", "E-CREATE").grants("R-APPROVE", "E-APPROVE")
    bank.assign("PAY-A1", "R-CREATE").assign("PAY-A1", "R-APPROVE")
    return bank


def test_a05_flags_sod_conflict(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    _sod_bank_simple(bank)
    data = bank.load(tmp_path)
    findings = SodConflictRule().run(data)
    entities = {f.entity for f in findings}
    assert "E1:SOD-PAY-01" in entities
    f = next(f for f in findings if f.entity == "E1:SOD-PAY-01")
    assert f.severity == Severity.CRITICAL


def test_a05_entity_format(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    _sod_bank_simple(bank)
    data = bank.load(tmp_path)
    findings = SodConflictRule().run(data)
    assert all(":" in f.entity for f in findings)
    assert any(f.entity == "E1:SOD-PAY-01" for f in findings)


def test_a05_near_miss_non_conflicting_functions_not_flagged(tmp_path):
    """Two functions that share no SoD rule → no finding."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1")
    bank.role("R-X").role("R-Y")
    bank.entitlement("E-X", "SOME_UNIQUE_FN_A")
    bank.entitlement("E-Y", "SOME_UNIQUE_FN_B")
    bank.grants("R-X", "E-X").grants("R-Y", "E-Y")
    bank.assign("PAY-A1", "R-X").assign("PAY-A1", "R-Y")
    data = bank.load(tmp_path)
    assert SodConflictRule().run(data) == []


def test_a05_conflict_via_3_level_inheritance_with_exact_path(tmp_path):
    """Conflict reachable only through 3 levels of role inheritance; path checked."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1")
    # Hierarchy: R1 → R2 → R3, where R3 grants PAYMENT_CREATE
    bank.role("R1").role("R2").role("R3")
    bank.contains("R1", "R2").contains("R2", "R3")
    bank.entitlement("E-CREATE", "PAYMENT_CREATE")
    bank.entitlement("E-APPROVE", "PAYMENT_APPROVE")
    bank.grants("R3", "E-CREATE")
    bank.grants("R1", "E-APPROVE")  # direct on R1
    bank.assign("PAY-A1", "R1")
    data = bank.load(tmp_path)

    findings = SodConflictRule().run(data)
    sod_pay01 = next((f for f in findings if f.entity == "E1:SOD-PAY-01"), None)
    assert sod_pay01 is not None, "Expected conflict E1:SOD-PAY-01 not found"

    path_create = sod_pay01.details["PAYMENT_CREATE"]
    path_approve = sod_pay01.details["PAYMENT_APPROVE"]

    assert path_create == ["PAY-A1", "R1", "R2", "R3", "E-CREATE"]
    assert path_approve == ["PAY-A1", "R1", "E-APPROVE"]


def test_a05_conflict_across_two_accounts(tmp_path):
    """Same employee holds conflicting functions on two separate accounts."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1").account("PAY-A2", employee_id="E1")
    bank.role("R-CREATE").role("R-APPROVE")
    bank.entitlement("E-CREATE", "PAYMENT_CREATE")
    bank.entitlement("E-APPROVE", "PAYMENT_APPROVE")
    bank.grants("R-CREATE", "E-CREATE").grants("R-APPROVE", "E-APPROVE")
    bank.assign("PAY-A1", "R-CREATE")
    bank.assign("PAY-A2", "R-APPROVE")
    data = bank.load(tmp_path)

    findings = SodConflictRule().run(data)
    assert any(f.entity == "E1:SOD-PAY-01" for f in findings)


def test_a05_near_miss_disabled_account_excluded(tmp_path):
    """Disabled account does not contribute to SoD analysis."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1", status="active")
    bank.account("PAY-A2", employee_id="E1", status="disabled")
    bank.role("R-CREATE").role("R-APPROVE")
    bank.entitlement("E-CREATE", "PAYMENT_CREATE")
    bank.entitlement("E-APPROVE", "PAYMENT_APPROVE")
    bank.grants("R-CREATE", "E-CREATE").grants("R-APPROVE", "E-APPROVE")
    bank.assign("PAY-A1", "R-CREATE")
    bank.assign("PAY-A2", "R-APPROVE")  # on disabled account
    data = bank.load(tmp_path)

    # Only PAYMENT_CREATE is reachable; no conflict
    sod_entities = {f.entity for f in SodConflictRule().run(data)}
    assert "E1:SOD-PAY-01" not in sod_entities


def test_a05_near_miss_expired_assignment_excluded(tmp_path):
    """An assignment that expired before as_of does not count."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1")
    bank.role("R-CREATE").role("R-APPROVE")
    bank.entitlement("E-CREATE", "PAYMENT_CREATE")
    bank.entitlement("E-APPROVE", "PAYMENT_APPROVE")
    bank.grants("R-CREATE", "E-CREATE").grants("R-APPROVE", "E-APPROVE")
    # R-CREATE expired one day before as_of
    bank.assign("PAY-A1", "R-CREATE", valid_from="2020-01-01", valid_to="2026-09-29")
    bank.assign("PAY-A1", "R-APPROVE")
    data = bank.load(tmp_path)

    sod_entities = {f.entity for f in SodConflictRule().run(data)}
    assert "E1:SOD-PAY-01" not in sod_entities


def test_a05_deterministic_path_selection_lex_min(tmp_path):
    """Two entitlements with same business function at equal depth: lex-min chosen."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1")
    bank.role("R1")
    bank.entitlement("E-CREATE-A", "PAYMENT_CREATE")
    bank.entitlement("E-CREATE-B", "PAYMENT_CREATE")
    bank.entitlement("E-APPROVE", "PAYMENT_APPROVE")
    bank.grants("R1", "E-CREATE-A").grants("R1", "E-CREATE-B").grants("R1", "E-APPROVE")
    bank.assign("PAY-A1", "R1")
    data = bank.load(tmp_path)

    findings = SodConflictRule().run(data)
    f = next(f for f in findings if f.entity == "E1:SOD-PAY-01")
    # E-CREATE-A < E-CREATE-B lexicographically
    assert f.details["PAYMENT_CREATE"][-1] == "E-CREATE-A"


def test_a05_severity_from_ruleset(tmp_path):
    """SOD-PAY-01 is CRITICAL per the bundled ruleset."""
    bank = MiniBank(as_of=AS_OF)
    _sod_bank_simple(bank)
    data = bank.load(tmp_path)
    f = next(f for f in SodConflictRule().run(data) if f.entity == "E1:SOD-PAY-01")
    assert f.severity == Severity.CRITICAL


# ===========================================================================
# A-06  recert_rubber_stamp
# ===========================================================================


def _fast_decisions(bank: MiniBank, n: int, *, campaign_id: str, reviewer: str) -> None:
    """Add n fast decisions (3 s each) for the given reviewer."""
    for i in range(n):
        bank.decision(
            campaign_id,
            f"PAY-ACC-{i}",
            f"R-{i}",
            reviewer,
            item_shown_at=f"2026-03-10T{10 + i // 60:02d}:{i % 60:02d}:00",
            decided_at=f"2026-03-10T{10 + i // 60:02d}:{i % 60:02d}:03",
        )


def test_a06_self_review_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("REV1")
    bank.account("PAY-REV", employee_id="REV1")
    bank.campaign("C1")
    # Reviewer decides 'keep' on their own account
    bank.decision("C1", "PAY-REV", "R1", "REV1", decision="keep")
    data = bank.load(tmp_path)

    findings = RecertRubberStamp().run(data)
    assert len(findings) == 1
    f = findings[0]
    assert f.entity == "C1:REV1"
    assert f.severity == Severity.HIGH
    assert "self_review" in f.details["triggers"]
    assert "PAY-REV" in f.details["self_reviewed_accounts"]


def test_a06_speed_flagged(tmp_path):
    """25 decisions, all under fast_seconds → speed trigger, MEDIUM severity."""
    bank = MiniBank(as_of=AS_OF)
    for i in range(25):
        bank.account(f"PAY-ACC-{i}", employee_id=f"EMP-{i}")
        bank.rows["hr_employees"].append(
            dict(
                employee_id=f"EMP-{i}",
                name=f"P{i}",
                org_unit="X",
                job_function="Y",
                entry_date="2020-01-01",
                exit_date="",
            )
        )
    bank.campaign("C1")
    _fast_decisions(bank, 25, campaign_id="C1", reviewer="REV2")
    data = bank.load(tmp_path)

    findings = RecertRubberStamp().run(data)
    assert any(f.entity == "C1:REV2" for f in findings)
    f = next(f for f in findings if f.entity == "C1:REV2")
    assert f.severity == Severity.MEDIUM
    assert "speed" in f.details["triggers"]


def test_a06_near_miss_slow_reviewer_not_flagged(tmp_path):
    """All decisions take 10 s each — above fast_seconds=5 → no speed trigger."""
    bank = MiniBank(as_of=AS_OF)
    for i in range(25):
        bank.account(f"PAY-ACC-{i}", employee_id=f"EMP-{i}")
        bank.rows["hr_employees"].append(
            dict(
                employee_id=f"EMP-{i}",
                name=f"P{i}",
                org_unit="X",
                job_function="Y",
                entry_date="2020-01-01",
                exit_date="",
            )
        )
    bank.campaign("C1")
    for i in range(25):
        bank.decision(
            "C1",
            f"PAY-ACC-{i}",
            f"R-{i}",
            "REV-SLOW",
            item_shown_at=f"2026-03-10T{10 + i // 60:02d}:{i % 60:02d}:00",
            decided_at=f"2026-03-10T{10 + i // 60:02d}:{i % 60:02d}:10",  # 10 s
        )
    data = bank.load(tmp_path)
    entities = {f.entity for f in RecertRubberStamp().run(data)}
    assert "C1:REV-SLOW" not in entities


def test_a06_near_miss_few_decisions_not_flagged(tmp_path):
    """Reviewer has only 5 fast decisions — below min_items=20 → no speed trigger."""
    bank = MiniBank(as_of=AS_OF)
    for i in range(5):
        bank.account(f"PAY-ACC-{i}", employee_id=f"EMP-{i}")
        bank.rows["hr_employees"].append(
            dict(
                employee_id=f"EMP-{i}",
                name=f"P{i}",
                org_unit="X",
                job_function="Y",
                entry_date="2020-01-01",
                exit_date="",
            )
        )
    bank.campaign("C1")
    _fast_decisions(bank, 5, campaign_id="C1", reviewer="REV3")
    data = bank.load(tmp_path)
    entities = {f.entity for f in RecertRubberStamp().run(data)}
    assert "C1:REV3" not in entities


def test_a06_revoke_own_account_not_self_review(tmp_path):
    """Reviewer votes 'revoke' on own account → NOT self-review."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("REV1")
    bank.account("PAY-REV", employee_id="REV1")
    bank.campaign("C1")
    bank.decision("C1", "PAY-REV", "R1", "REV1", decision="revoke")
    data = bank.load(tmp_path)
    entities = {f.entity for f in RecertRubberStamp().run(data)}
    assert "C1:REV1" not in entities


def test_a06_both_triggers_gives_high_severity(tmp_path):
    """Both self-review and speed → HIGH severity."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("REV1")
    bank.account("PAY-REV", employee_id="REV1")
    for i in range(24):
        bank.account(f"PAY-O-{i}", employee_id=f"EO-{i}")
        bank.rows["hr_employees"].append(
            dict(
                employee_id=f"EO-{i}",
                name=f"P{i}",
                org_unit="X",
                job_function="Y",
                entry_date="2020-01-01",
                exit_date="",
            )
        )
    bank.campaign("C1")
    # self-review keep
    bank.decision(
        "C1",
        "PAY-REV",
        "R-0",
        "REV1",
        decision="keep",
        item_shown_at="2026-03-10T10:00:00",
        decided_at="2026-03-10T10:00:03",
    )
    # 24 more fast decisions
    for i in range(24):
        bank.decision(
            "C1",
            f"PAY-O-{i}",
            f"R-{i}",
            "REV1",
            item_shown_at=f"2026-03-10T10:{i + 1:02d}:00",
            decided_at=f"2026-03-10T10:{i + 1:02d}:03",
        )
    data = bank.load(tmp_path)
    f = next(f for f in RecertRubberStamp().run(data) if f.entity == "C1:REV1")
    assert f.severity == Severity.HIGH
    assert set(f.details["triggers"]) == {"self_review", "speed"}


def test_a06_details_keys_present(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("REV1")
    bank.account("PAY-REV", employee_id="REV1")
    bank.campaign("C1")
    bank.decision("C1", "PAY-REV", "R1", "REV1", decision="keep")
    data = bank.load(tmp_path)
    f = RecertRubberStamp().run(data)[0]
    assert set(f.details) >= {"triggers", "decisions", "fast_decisions", "self_reviewed_accounts"}


def test_a06_entity_format(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("REV1")
    bank.account("PAY-REV", employee_id="REV1")
    bank.campaign("CAM-42")
    bank.decision("CAM-42", "PAY-REV", "R1", "REV1", decision="keep")
    data = bank.load(tmp_path)
    assert RecertRubberStamp().run(data)[0].entity == "CAM-42:REV1"


# ===========================================================================
# A-07  orphan_account
# ===========================================================================


def test_a07_flags_empty_employee_id(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", account_type="personal", status="active", employee_id="")
    data = bank.load(tmp_path)
    findings = OrphanAccount().run(data)
    assert len(findings) == 1
    assert findings[0].entity == "PAY-1"
    assert findings[0].severity == Severity.HIGH


def test_a07_flags_unknown_employee_id(tmp_path):
    """Account has employee_id but that employee is not in HR → orphan."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", account_type="personal", status="active", employee_id="GHOST-99")
    data = bank.load(tmp_path)
    findings = OrphanAccount().run(data)
    assert any(f.entity == "PAY-1" for f in findings)


def test_a07_near_miss_valid_employee_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-1", account_type="personal", status="active", employee_id="E1")
    data = bank.load(tmp_path)
    assert OrphanAccount().run(data) == []


def test_a07_near_miss_disabled_account_not_flagged(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("PAY-1", account_type="personal", status="disabled", employee_id="")
    data = bank.load(tmp_path)
    assert OrphanAccount().run(data) == []


def test_a07_technical_account_not_flagged(tmp_path):
    """technical accounts are not expected to have an employee_id."""
    bank = MiniBank(as_of=AS_OF)
    bank.account("SVC-1", account_type="technical", status="active", employee_id="")
    data = bank.load(tmp_path)
    assert OrphanAccount().run(data) == []


def test_a07_entity_format(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.account("GL-007", account_type="personal", status="active", employee_id="")
    data = bank.load(tmp_path)
    assert OrphanAccount().run(data)[0].entity == "GL-007"


# ===========================================================================
# A-08  unusual_role
# ===========================================================================


def _peer_group(bank: MiniBank, n: int, *, role_for_first: bool = True) -> list[str]:
    """Create n employees in the same peer group; first one gets R-RARE if role_for_first."""
    emp_ids = [f"E-PEER-{i}" for i in range(n)]
    acct_ids = [f"PAY-PEER-{i}" for i in range(n)]
    bank.role("R-RARE")
    for i, (eid, aid) in enumerate(zip(emp_ids, acct_ids, strict=True)):
        bank.employee(eid, org_unit="Unit-X", job_function="Analyst")
        bank.account(aid, employee_id=eid, system="PAY")
        if i == 0 and role_for_first:
            bank.assign(aid, "R-RARE")
    return acct_ids


def test_a08_flags_unusual_role(tmp_path):
    """1 out of 10 peers holds R-RARE → 10% = rare_share default 0.1 → not flagged."""
    # With 10 peers and 1 holder: 1/10 = 0.1 which equals rare_share → NOT flagged
    # Need stricter: 1 out of 11 peers (< 10%)
    bank = MiniBank(as_of=AS_OF)
    n = 11
    bank.role("R-RARE")
    for i in range(n):
        eid, aid = f"E-{i}", f"PAY-{i}"
        bank.employee(eid, org_unit="UX", job_function="Analyst")
        bank.account(aid, employee_id=eid)
        if i == 0:
            bank.assign(aid, "R-RARE")
    data = bank.load(tmp_path)
    findings = UnusualRole().run(data)
    unusual = [f for f in findings if "R-RARE" in f.entity]
    assert len(unusual) == 1
    assert unusual[0].entity == "PAY-0:R-RARE"
    assert unusual[0].severity == Severity.LOW


def test_a08_near_miss_below_min_peers_not_flagged(tmp_path):
    """Peer group of 4 — below min_peers=5 → not assessed."""
    bank = MiniBank(as_of=AS_OF)
    _peer_group(bank, 4)
    data = bank.load(tmp_path)
    assert UnusualRole().run(data) == []


def test_a08_near_miss_common_role_not_flagged(tmp_path):
    """5 peers all hold R-RARE → 100% share → not unusual."""
    bank = MiniBank(as_of=AS_OF)
    bank.role("R-RARE")
    for i in range(5):
        eid, aid = f"E-{i}", f"PAY-{i}"
        bank.employee(eid, org_unit="UX", job_function="Analyst")
        bank.account(aid, employee_id=eid)
        bank.assign(aid, "R-RARE")
    data = bank.load(tmp_path)
    unusual = [f for f in UnusualRole().run(data) if "R-RARE" in f.entity]
    assert unusual == []


def test_a08_leaver_excluded_from_peer_group(tmp_path):
    """Leaver is excluded from the peer group; group below min_peers → not assessed."""
    bank = MiniBank(as_of=AS_OF)
    bank.role("R-RARE")
    # 4 active + 1 leaver  →  effective peer group = 4 < min_peers
    for i in range(4):
        eid, aid = f"E-{i}", f"PAY-{i}"
        bank.employee(eid, org_unit="UX", job_function="Analyst")
        bank.account(aid, employee_id=eid)
        if i == 0:
            bank.assign(aid, "R-RARE")
    bank.employee("E-GONE", org_unit="UX", job_function="Analyst", exit_date="2026-01-01")
    bank.account("PAY-GONE", employee_id="E-GONE")
    data = bank.load(tmp_path)
    assert UnusualRole().run(data) == []


def test_a08_details_keys(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.role("R-RARE")
    for i in range(11):
        eid, aid = f"E-{i}", f"PAY-{i}"
        bank.employee(eid, org_unit="UX", job_function="Analyst")
        bank.account(aid, employee_id=eid)
        if i == 0:
            bank.assign(aid, "R-RARE")
    data = bank.load(tmp_path)
    findings = [f for f in UnusualRole().run(data) if "R-RARE" in f.entity]
    assert findings
    f = findings[0]
    assert "peer_group_size" in f.details
    assert "peers_with_role" in f.details
    assert f.details["peer_group_size"] == 11
    assert f.details["peers_with_role"] == 1


def test_a08_entity_format(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.role("R-RARE")
    for i in range(11):
        eid, aid = f"E-{i}", f"PAY-{i}"
        bank.employee(eid, org_unit="UX", job_function="Analyst")
        bank.account(aid, employee_id=eid)
        if i == 0:
            bank.assign(aid, "R-RARE")
    data = bank.load(tmp_path)
    f = [f for f in UnusualRole().run(data) if "R-RARE" in f.entity][0]
    acct_id, role_id = f.entity.split(":")
    assert acct_id.startswith("PAY-")
    assert role_id == "R-RARE"


# ===========================================================================
# Registry and run_access_rules
# ===========================================================================


def test_access_rules_registry_has_all_eight_rules():
    assert set(ACCESS_RULES.ids()) == {
        "A-01",
        "A-02",
        "A-03",
        "A-04",
        "A-05",
        "A-06",
        "A-07",
        "A-08",
    }


def test_run_access_rules_findings_include_as_of_in_params(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)
    findings = run_access_rules(data)
    # A-01 should fire; verify as_of in its params
    a01 = [f for f in findings if f.rule_id == "A-01"]
    assert a01, "Expected at least one A-01 finding"
    for f in a01:
        assert f.params.get("as_of") == AS_OF.isoformat()


def test_no_rule_mutates_access_data_frames(tmp_path):
    """Running all rules must not modify any DataFrame stored on AccessData."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-1", employee_id="E1")
    bank.role("R1")
    bank.assign("PAY-1", "R1")
    data = bank.load(tmp_path)

    before_accts = data.accounts.copy(deep=True)
    before_emps = data.employees.copy(deep=True)

    run_access_rules(data)

    pd.testing.assert_frame_equal(data.accounts, before_accts)
    pd.testing.assert_frame_equal(data.employees, before_emps)


def test_run_access_rules_empty_bank_no_findings(tmp_path):
    """A completely empty bank should produce zero findings."""
    data = MiniBank(as_of=AS_OF).load(tmp_path)
    assert run_access_rules(data) == []


# ===========================================================================
# load_sod_ruleset and employee_functions unit tests
# ===========================================================================


def test_load_sod_ruleset_returns_sod_conflicts():
    conflicts = load_sod_ruleset()
    assert len(conflicts) >= 1
    c = conflicts[0]
    assert hasattr(c, "id")
    assert isinstance(c.functions, tuple) and len(c.functions) == 2
    assert isinstance(c.severity, Severity)


def test_employee_functions_returns_paths(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1")
    bank.role("R1")
    bank.entitlement("ENT-1", "PAYMENT_CREATE")
    bank.grants("R1", "ENT-1")
    bank.assign("PAY-A1", "R1")
    data = bank.load(tmp_path)
    fns = employee_functions(data)
    assert "E1" in fns
    assert "PAYMENT_CREATE" in fns["E1"]
    assert fns["E1"]["PAYMENT_CREATE"] == ["PAY-A1", "R1", "ENT-1"]


def test_employee_functions_skips_empty_employee_id(tmp_path):
    """Active personal account with empty employee_id is silently skipped."""
    bank = MiniBank(as_of=AS_OF)
    # Orphan account: no employee_id
    bank.account("PAY-ORPHAN", account_type="personal", status="active", employee_id="")
    bank.role("R1")
    bank.entitlement("ENT-1", "PAYMENT_CREATE")
    bank.grants("R1", "ENT-1")
    bank.assign("PAY-ORPHAN", "R1")
    data = bank.load(tmp_path)
    fns = employee_functions(data)
    assert "" not in fns  # empty emp_id must not produce an entry


def test_employee_functions_skips_unknown_role_id(tmp_path):
    """Assignment referencing a role not in the graph is silently skipped."""
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1")
    # Deliberately add an assignment without the corresponding role row
    bank.rows["account_roles"].append(
        dict(account_id="PAY-A1", role_id="R-GHOST", valid_from="2020-01-01", valid_to="")
    )
    data = bank.load(tmp_path)
    fns = employee_functions(data)
    # R-GHOST is not in the graph → no functions found for E1
    assert not fns.get("E1")


def test_employee_functions_excludes_disabled_accounts(tmp_path):
    bank = MiniBank(as_of=AS_OF)
    bank.employee("E1")
    bank.account("PAY-A1", employee_id="E1", status="disabled")
    bank.role("R1")
    bank.entitlement("ENT-1", "PAYMENT_CREATE")
    bank.grants("R1", "ENT-1")
    bank.assign("PAY-A1", "R1")
    data = bank.load(tmp_path)
    fns = employee_functions(data)
    assert "E1" not in fns or "PAYMENT_CREATE" not in fns.get("E1", {})


def test_a08_peers_without_accounts_still_count(tmp_path):
    """Spec: the peer group is all employees who have not left with the same
    org_unit and job_function, not only those who happen to have an account.
    10 peers in HR, only E0 has an account (with the role): 1/10 is not < 0.1,
    so nothing is flagged. Counting only account holders would give 1/1."""
    bank = MiniBank()
    bank.role("R-X")
    for i in range(10):
        bank.employee(f"E{i}", org_unit="Kredit", job_function="Analyse")
    bank.account("ACC-E0", employee_id="E0")
    bank.assign("ACC-E0", "R-X")
    data = bank.load(tmp_path)
    assert UnusualRole().run(data) == []

    bank2 = MiniBank()
    bank2.role("R-X")
    for i in range(11):
        bank2.employee(f"E{i}", org_unit="Kredit", job_function="Analyse")
    bank2.account("ACC-E0", employee_id="E0")
    bank2.assign("ACC-E0", "R-X")
    (f,) = UnusualRole().run(bank2.load(tmp_path / "b2"))
    assert f.details == {"peer_group_size": 11, "peers_with_role": 1}
