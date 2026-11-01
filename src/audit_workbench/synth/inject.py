"""Injection of the known issues recorded in ``ground_truth.json``.

``inject_issues(bank)`` mutates a clean bank so that each of the rules
A-01 to A-08 fires exactly the number of times given in spec section 5:

======  ====  ----------------------------------------------------------------
Rule    N     Notes
======  ====  ----------------------------------------------------------------
A-01    12    4 of them logged in after the exit date
A-02    10    6 stale last_login, 4 never logged in with old created_at
A-03    3     generic accounts switched to interactive login
A-04    6     2 privileged only via inheritance (R-*-SUPERVISOR)
A-05    9     3 only through 3-level inheritance, 2 across two accounts
A-06    3     1 self-review, 2 speed
A-07    4     2 with empty owner, 2 pointing to an unknown employee
A-08    8     one unusual role per large peer group
======  ====  ----------------------------------------------------------------

Injection targets are chosen deterministically and kept disjoint so that an
injection never triggers a second rule (no collateral findings).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from audit_workbench.synth.bank import Bank, _ts

VALID_FROM_INJECTED = "2026-07-01"
CREATED_INJECTED = "2026-07-01T08:00:00"

EXPECTED_COUNTS = {
    "A-01": 12,
    "A-02": 10,
    "A-03": 3,
    "A-04": 6,
    "A-05": 9,
    "A-06": 3,
    "A-07": 4,
    "A-08": 8,
}


def _pick(bank: Bank, unit: str, func: str, n: int, offset: int = 0) -> list[str]:
    """Deterministically take *n* unused employees from a peer group."""
    picked = [e for e in bank.groups[(unit, func)][offset:] if e not in bank.used_employees][:n]
    if len(picked) < n:
        raise RuntimeError(f"not enough injection targets in {unit}/{func}")
    bank.used_employees.update(picked)
    return picked


def _set_exit(bank: Bank, emp_id: str, exit_date) -> None:
    bank.emp_meta[emp_id]["exit_date"] = exit_date
    row = next(r for r in bank.rows["hr_employees"] if r["employee_id"] == emp_id)
    row["exit_date"] = exit_date.isoformat()


def _set_last_login(bank: Bank, account_id: str, value: str) -> None:
    row = next(r for r in bank.rows["accounts"] if r["account_id"] == account_id)
    row["last_login"] = value


def _account_of(bank: Bank, emp_id: str) -> str:
    return next(a for a, m in bank.acct_meta.items() if m["employee_id"] == emp_id)


def _inject_leavers(bank: Bank) -> list[dict[str, str]]:
    """A-01: 12 leavers whose accounts stay active; 4 logged in after exit."""
    issues: list[dict[str, str]] = []
    plan = [
        ("Zahlungsverkehr", "Sachbearbeitung", 3),
        ("Kredit", "Sachbearbeitung", 3),
        ("Rechnungswesen", "Sachbearbeitung", 2),
        ("Handel", "Sachbearbeitung", 2),
        ("Entwicklung", "Sachbearbeitung", 2),
    ]
    targets: list[tuple[str, Any]] = []
    for unit, func, n in plan:
        for emp_id in _pick(bank, unit, func, n):
            exit_day = bank.as_of - timedelta(days=int(bank.rng.integers(40, 200)))
            targets.append((emp_id, exit_day))
    for idx, (emp_id, exit_day) in enumerate(targets):
        _set_exit(bank, emp_id, exit_day)
        account_id = _account_of(bank, emp_id)
        if idx < 4:  # logged in after the exit date
            login = _ts(exit_day + timedelta(days=3), 10, 30)
            _set_last_login(bank, account_id, login)
            note = (
                f"Account {account_id} of leaver {emp_id} (exit {exit_day}) "
                f"is still active and was used after the exit"
            )
        elif idx < 8:
            _set_last_login(bank, account_id, "")
            note = f"Account {account_id} of leaver {emp_id} (exit {exit_day}) is still active"
        else:
            _set_last_login(bank, account_id, _ts(exit_day - timedelta(days=10), 9, 0))
            note = f"Account {account_id} of leaver {emp_id} (exit {exit_day}) is still active"
        issues.append({"rule_id": "A-01", "entity": account_id, "note": note})
    return issues


def _inject_dormant(bank: Bank) -> list[dict[str, str]]:
    """A-02: 10 active interactive accounts unused for more than 90 days."""
    issues: list[dict[str, str]] = []
    plan = [
        ("Zahlungsverkehr", "Sachbearbeitung", 2),
        ("Kredit", "Sachbearbeitung", 2),
        ("Rechnungswesen", "Sachbearbeitung", 2),
        ("Handel", "Sachbearbeitung", 2),
        ("IT-Betrieb", "Sachbearbeitung", 2),
    ]
    stale = 0
    for unit, func, n in plan:
        for emp_id in _pick(bank, unit, func, n):
            account_id = _account_of(bank, emp_id)
            if stale < 6:
                days = int(bank.rng.integers(100, 300))
                _set_last_login(bank, account_id, _ts(bank.as_of - timedelta(days=days), 8, 5))
                note = f"Account {account_id} unused for {days} days"
            else:
                _set_last_login(bank, account_id, "")
                note = f"Account {account_id} was created long ago and never used"
            stale += 1
            issues.append({"rule_id": "A-02", "entity": account_id, "note": note})
    return issues


def _inject_generic(bank: Bank) -> list[dict[str, str]]:
    """A-03: 3 generic accounts with interactive login."""
    issues: list[dict[str, str]] = []
    for idx, system in enumerate(["PAY", "CORE", "TRADE"]):
        account_id = bank.add_account(
            system,
            account_type="generic",
            interactive=True,
            created_at=CREATED_INJECTED,
            last_login=_ts(bank.as_of - timedelta(days=2 + idx), 8, 40),
        )
        bank.assign(account_id, _view_role_of(system), valid_from=VALID_FROM_INJECTED)
        issues.append(
            {
                "rule_id": "A-03",
                "entity": account_id,
                "note": f"Generic account {account_id} allows interactive login",
            }
        )
    return issues


def _view_role_of(system: str) -> str:
    return {
        "PAY": "R-PAY-VIEW",
        "CORE": "R-CRD-VIEW",
        "GL": "R-GL-VIEW",
        "TRADE": "R-TRD-VIEW",
        "AD": "R-AD-RO",
    }[system]


def _inject_unreviewed(bank: Bank) -> list[dict[str, str]]:
    """A-04: 6 privileged assignments without any recertification decision.

    2 on personal accounts are privileged only through inheritance
    (R-PAY-SUPERVISOR / R-CRD-SUPERVISOR contain a privileged child role),
    4 sit on technical accounts with directly privileged roles.
    """
    issues: list[dict[str, str]] = []
    inherited = [
        ("Zahlungsverkehr", "Sonderaufgaben", "R-PAY-SUPERVISOR"),
        ("Kredit", "Sonderaufgaben", "R-CRD-SUPERVISOR"),
    ]
    for unit, func, role_id in inherited:
        (emp_id,) = _pick(bank, unit, func, 1)
        account_id = _account_of(bank, emp_id)
        bank.assign(account_id, role_id, valid_from=VALID_FROM_INJECTED)
        issues.append(
            {
                "rule_id": "A-04",
                "entity": f"{account_id}:{role_id}",
                "note": (
                    f"Role {role_id} on {account_id} is privileged only through "
                    f"inheritance and has no recertification decision"
                ),
            }
        )
    for system, role_id in [
        ("PAY", "R-PAY-TEAMLEAD"),
        ("CORE", "R-CRD-LIMIT"),
        ("GL", "R-GL-APPROVER"),
        ("AD", "R-AD-ADMIN"),
    ]:
        account_id = bank.add_account(
            system,
            account_type="technical",
            interactive=False,
            created_at=CREATED_INJECTED,
            last_login="",
        )
        bank.assign(account_id, role_id, valid_from=VALID_FROM_INJECTED)
        issues.append(
            {
                "rule_id": "A-04",
                "entity": f"{account_id}:{role_id}",
                "note": (
                    f"Privileged role {role_id} on technical account {account_id} "
                    f"has no recertification decision"
                ),
            }
        )
    return issues


def _inject_sod(bank: Bank) -> list[dict[str, str]]:
    """A-05: 9 employees holding both functions of a conflict.

    * 4 same-account pairs (one per conflict domain),
    * 2 pairs spread across two accounts of the same employee,
    * 3 pairs detectable only through the 3-level GL inheritance chain.
    """
    issues: list[dict[str, str]] = []

    def sod(emp_id: str, conflict_id: str, fn_a: str, acct_a: str, fn_b: str, acct_b: str) -> None:
        cross = " on a second account" if acct_a != acct_b else ""
        issues.append(
            {
                "rule_id": "A-05",
                "entity": f"{emp_id}:{conflict_id}",
                "note": (
                    f"Employee {emp_id} holds {fn_a} on {acct_a} and {fn_b} "
                    f"on {acct_b}{cross} ({conflict_id})"
                ),
            }
        )

    # Same account: create + approve on one account each.
    same_account = [
        (
            "Zahlungsverkehr",
            "Sonderaufgaben",
            1,
            "R-PAY-CLERK",
            "R-PAY-TEAMLEAD",
            "SOD-PAY-01",
            "PAYMENT_CREATE",
            "PAYMENT_APPROVE",
            True,
        ),
        (
            "Kredit",
            "Sonderaufgaben",
            1,
            "R-CRD-CLERK",
            "R-CRD-APPROVER",
            "SOD-CRD-01",
            "CREDIT_ENTER",
            "CREDIT_APPROVE",
            True,
        ),
        (
            "Rechnungswesen",
            "Sonderaufgaben",
            0,
            "R-GL-CLERK",
            "R-GL-APPROVER",
            "SOD-GL-01",
            "GL_POST",
            "GL_APPROVE",
            True,
        ),
        (
            "Handel",
            "Sonderaufgaben",
            0,
            "R-TRD-TRADER",
            "R-TRD-SETTLER",
            "SOD-TRD-01",
            "TRADE_EXECUTE",
            "TRADE_SETTLE",
            False,
        ),
    ]
    for unit, func, offset, role_a, role_b, cid, fn_a, fn_b, b_priv in same_account:
        (emp_id,) = _pick(bank, unit, func, 1, offset=offset)
        account_id = _account_of(bank, emp_id)
        bank.assign(account_id, role_a, valid_from=VALID_FROM_INJECTED)
        bank.assign(account_id, role_b, valid_from=VALID_FROM_INJECTED)
        if b_priv:
            bank.auto_decision(account_id, role_b)
        sod(emp_id, cid, fn_a, account_id, fn_b, account_id)

    # Across two accounts of the same employee.
    for unit, func, role_a, role_b in [
        ("Zahlungsverkehr", "Sonderaufgaben", "R-PAY-CLERK", "R-PAY-TEAMLEAD"),
        ("Rechnungswesen", "Sonderaufgaben", "R-GL-CLERK", "R-GL-APPROVER"),
    ]:
        (emp_id,) = _pick(bank, unit, func, 1)
        system = bank.emp_meta[emp_id]["system"]
        account_a = _account_of(bank, emp_id)
        bank.assign(account_a, role_a, valid_from=VALID_FROM_INJECTED)
        account_b = bank.add_account(
            system,
            employee_id=emp_id,
            created_at=CREATED_INJECTED,
            last_login=bank.personal_login(),
        )
        bank.assign(account_b, role_b, valid_from=VALID_FROM_INJECTED)
        privileged = ROLES_PRIV[role_b]
        if privileged:
            bank.auto_decision(account_b, role_b)
        fn_a = _fn_of(role_a)
        fn_b = _fn_of(role_b)
        cid = "SOD-PAY-01" if system == "PAY" else "SOD-GL-01"
        sod(emp_id, cid, fn_a, account_a, fn_b, account_b)

    # Only detectable through the 3-level chain
    # R-GL-MANAGER -> R-GL-TEAMLEAD -> R-GL-CLERK (GL_POST at the bottom).
    for emp_id in _pick(bank, "Rechnungswesen", "Sonderaufgaben", 3):
        account_id = _account_of(bank, emp_id)
        bank.assign(account_id, "R-GL-MANAGER", valid_from=VALID_FROM_INJECTED)
        bank.assign(account_id, "R-GL-APPROVER", valid_from=VALID_FROM_INJECTED)
        bank.auto_decision(account_id, "R-GL-APPROVER")
        sod(emp_id, "SOD-GL-01", "GL_POST", account_id, "GL_APPROVE", account_id)
    return issues


ROLES_PRIV = {
    "R-PAY-TEAMLEAD": True,
    "R-CRD-APPROVER": True,
    "R-GL-APPROVER": True,
    "R-TRD-LEAD": True,
    "R-AD-ADMIN": True,
    "R-CRD-LIMIT": True,
}


def _fn_of(role_id: str) -> str:
    mapping = {
        "R-PAY-CLERK": "PAYMENT_CREATE",
        "R-CRD-CLERK": "CREDIT_ENTER",
        "R-GL-CLERK": "GL_POST",
        "R-TRD-TRADER": "TRADE_EXECUTE",
        "R-PAY-TEAMLEAD": "PAYMENT_APPROVE",
        "R-CRD-APPROVER": "CREDIT_APPROVE",
        "R-GL-APPROVER": "GL_APPROVE",
        "R-TRD-SETTLER": "TRADE_SETTLE",
    }
    return mapping[role_id]


def _inject_recert(bank: Bank) -> list[dict[str, str]]:
    """A-06: 1 self-review and 2 speed reviewers."""
    issues: list[dict[str, str]] = []

    # Self-review: a CAM-PAY reviewer decides keep on their own account.
    reviewer = bank.reviewer_pool["PAY"][0]
    own_account = _account_of(bank, reviewer)
    row = next(
        r
        for r in bank.rows["recert_decisions"]
        if r["campaign_id"] == "CAM-PAY" and r["account_id"] == own_account
    )
    row["reviewer_employee_id"] = reviewer
    row["decision"] = "keep"
    issues.append(
        {
            "rule_id": "A-06",
            "entity": f"CAM-PAY:{reviewer}",
            "note": f"Reviewer {reviewer} approved their own account {own_account}",
        }
    )

    # Speed: the two reviewers with the most decisions in CAM-CORE and
    # CAM-TRADE decide everything within 2-4 seconds.
    for campaign_id, _system in [("CAM-CORE", "CORE"), ("CAM-TRADE", "TRADE")]:
        counts: dict[str, int] = {}
        for r in bank.rows["recert_decisions"]:
            if r["campaign_id"] == campaign_id:
                counts[r["reviewer_employee_id"]] = counts.get(r["reviewer_employee_id"], 0) + 1
        fast_reviewer = max(sorted(counts), key=lambda e: counts[e])
        if counts[fast_reviewer] < 20:
            raise RuntimeError(f"speed reviewer has only {counts[fast_reviewer]} decisions")
        fast = 0
        for r in bank.rows["recert_decisions"]:
            if r["campaign_id"] == campaign_id and r["reviewer_employee_id"] == fast_reviewer:
                shown_dt = datetime.strptime(r["item_shown_at"], "%Y-%m-%dT%H:%M:%S")
                r["decided_at"] = (shown_dt + timedelta(seconds=2 + fast % 3)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                )
                fast += 1
        issues.append(
            {
                "rule_id": "A-06",
                "entity": f"{campaign_id}:{fast_reviewer}",
                "note": (
                    f"Reviewer {fast_reviewer} decided {counts[fast_reviewer]} items "
                    f"in under 5 seconds each in {campaign_id}"
                ),
            }
        )
    return issues


def _inject_orphans(bank: Bank) -> list[dict[str, str]]:
    """A-07: 4 personal accounts without a matching HR employee."""
    issues: list[dict[str, str]] = []
    plan = [("PAY", ""), ("PAY", "E99998"), ("GL", ""), ("GL", "E99999")]
    for system, emp_id in plan:
        account_id = bank.add_account(
            system,
            employee_id=emp_id,
            created_at=CREATED_INJECTED,
            last_login=bank.personal_login(),
        )
        bank.assign(account_id, _view_role_of(system), valid_from=VALID_FROM_INJECTED)
        reason = "no owner" if not emp_id else f"owner {emp_id} is not in HR"
        issues.append(
            {
                "rule_id": "A-07",
                "entity": account_id,
                "note": f"Personal account {account_id} has {reason}",
            }
        )
    return issues


def _inject_unusual(bank: Bank) -> list[dict[str, str]]:
    """A-08: 8 employees holding a role almost none of their peers hold."""
    issues: list[dict[str, str]] = []
    plan = [
        ("Zahlungsverkehr", "Sachbearbeitung", "R-PAY-BENE", False),
        ("Kredit", "Sachbearbeitung", "R-CRD-VIEW", False),
        ("Rechnungswesen", "Sachbearbeitung", "R-GL-VIEW", False),
        ("Handel", "Sachbearbeitung", "R-TRD-VIEW", False),
        ("Handel", "Analyse", "R-TRD-VIEW", False),
        ("Kredit", "Analyse", "R-CRD-VIEW", False),
        ("IT-Betrieb", "Sachbearbeitung", "R-AD-ADMIN", True),
        ("Entwicklung", "Sachbearbeitung", "R-AD-ADMIN", True),
    ]
    for unit, func, role_id, privileged in plan:
        (emp_id,) = _pick(bank, unit, func, 1)
        account_id = _account_of(bank, emp_id)
        bank.assign(account_id, role_id, valid_from=VALID_FROM_INJECTED)
        if privileged:
            bank.auto_decision(account_id, role_id)
        peers = len(bank.groups[(unit, func)])
        issues.append(
            {
                "rule_id": "A-08",
                "entity": f"{account_id}:{role_id}",
                "note": (
                    f"Role {role_id} on {account_id} is held by only 1 of "
                    f"{peers} peers in {unit}/{func}"
                ),
            }
        )
    return issues


def inject_issues(bank: Bank) -> list[dict[str, str]]:
    """Apply every injection and return the ground-truth issue rows."""
    issues: list[dict[str, str]] = []
    issues += _inject_leavers(bank)
    issues += _inject_dormant(bank)
    issues += _inject_generic(bank)
    issues += _inject_unreviewed(bank)
    issues += _inject_sod(bank)
    issues += _inject_orphans(bank)
    issues += _inject_unusual(bank)
    issues += _inject_recert(bank)  # last: speeds up final decision timings
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["rule_id"]] = counts.get(issue["rule_id"], 0) + 1
    if counts != EXPECTED_COUNTS:
        raise RuntimeError(f"injected counts {counts} != spec {EXPECTED_COUNTS}")
    return issues
