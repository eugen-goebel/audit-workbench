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

import numpy as np

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


# ---------------------------------------------------------------------------
# Module B: journal injections
# ---------------------------------------------------------------------------

#: fee tariffs an invented fee income account is fed with (B-02)
FEE_TARIFFS = [
    1_490,
    2_900,
    4_950,
    7_900,
    9_900,
    12_500,
    19_900,
    24_900,
    29_900,
    39_900,
    49_900,
    99_900,
]

JOURNAL_COUNTS = {
    "B-01": 1,
    "B-02": 1,
    "B-03": 2,
    "B-04": 6,
    "B-05": 8,
    "B-06": 4,
    "B-07": 5,
    "B-08": 6,
    "B-09": 5,
    "B-10": 4,
}


def _free(
    ledger,  # noqa: ANN001
    *,
    minimum: int | None = None,
    single: bool = False,
    skip_preparer: str = "",
):  # noqa: ANN202
    out = []
    for entry in ledger.entries:
        if entry.kind != "manual" or entry.frozen:
            continue
        if single and len(entry.segments) != 1:
            continue
        if minimum is not None and entry.total < minimum:
            continue
        if skip_preparer and entry.preparer == skip_preparer:
            continue
        out.append(entry)
    return out


def _take(rng, pool, count: int):  # noqa: ANN001,ANN202
    picks = rng.choice(np.array(len(pool)), size=count, replace=False)
    return [pool[int(i)] for i in sorted(picks)]


def _invented_amount(rng) -> int:  # noqa: ANN001
    """Whole amount with a uniform first digit (invents a Benford departure)."""
    digit = int(rng.integers(1, 10))
    exponent = int(rng.integers(2, 6))
    return max(digit * 10**exponent + int(rng.integers(0, 10**exponent)), 1000)


def _issue(rule_id: str, note: str, entry: object | None = None) -> dict[str, object]:
    return {"rule_id": rule_id, "entity": "", "note": note, "entry": entry}


def inject_journal(bank: Bank, ledger, rng: np.random.Generator) -> list[dict[str, object]]:
    """Inject the journal issues of spec section 4 and return the ground truth.

    The entities of all issues except B-01/B-02/B-03 are document numbers that
    only exist after ``finalize_ledger``; they carry the entry in a private
    key that ``resolve_journal_issues`` turns into the entity.
    """
    from audit_workbench.synth.ledger import (  # noqa: PLC0415
        ACCOUNT_CLASS,
        B04_HOLIDAYS,
        FEE_ACCOUNT,
        FEE_SEGMENTS,
        MANUAL_LIMIT,
        RARE_PAIRS,
        ROUND_STEP,
        WEEKEND_DAYS,
        People,
        Segment,
        _to_round,
        cross_account_employee,
        first_workdays,
        is_holiday,
        last_workday_before,
        mad_first,
        month_end,
        previous_period,
    )

    people = People.from_bank(bank)
    issues: list[dict[str, object]] = []
    clerk_accounts = [people.personal[c] for c in ledger.clerks]
    near_miss_clerk = clerk_accounts[1]
    b01_clerk = clerk_accounts[9]
    b03_preparers = [clerk_accounts[4], clerk_accounts[6]]

    # -- B-03: two preparers with far too many round amounts --------------
    for account in b03_preparers:
        entries = [e for e in ledger.entries if e.preparer == account and not e.frozen]
        large = [e for e in entries if e.total >= ROUND_STEP]
        if len(large) < 30:
            raise RuntimeError(f"B-03 preparer {account} has only {len(large)} large entries")
        targets = [e for e in large if len(e.segments) == 1 and e.total % ROUND_STEP != 0]
        already = sum(1 for e in large if e.total % ROUND_STEP == 0)
        need = min(max(int(0.4 * len(large)) - already, 1), len(targets))
        for entry in _take(rng, targets, need):
            entry.frozen = True
            _to_round(entry)
        rounded = sum(1 for e in large if e.total % ROUND_STEP == 0)
        employee = people.employee_of(account)
        issues.append(
            {
                "rule_id": "B-03",
                "entity": employee,
                "note": (
                    f"{rounded} of the {len(large)} entries >= 1000.00 of {employee} "
                    f"({account}) are round amounts (share {rounded / len(large):.2f})"
                ),
            }
        )

    # -- B-01: one preparer whose amounts are invented --------------------
    edited = [e for e in ledger.entries if e.preparer == b01_clerk and not e.frozen]
    for entry in edited:
        entry.frozen = True
        for seg in entry.segments:
            seg.amount = _invented_amount(rng)
    employee = people.employee_of(b01_clerk)
    amounts = [s.amount for e in edited for s in e.segments]
    issues.append(
        {
            "rule_id": "B-01",
            "entity": employee,
            "note": (
                f"The {2 * len(amounts)} line amounts of {employee} ({b01_clerk}) have "
                f"uniformly distributed first digits (MAD {mad_first(amounts):.3f})"
            ),
        }
    )

    # -- B-02: one fee income account fed with tariff amounts -------------
    fee_entries = sorted(
        (
            e
            for e in ledger.entries
            if e.kind == "auto"
            and len(e.segments) == FEE_SEGMENTS
            and all(seg.credit == FEE_ACCOUNT for seg in e.segments)
        ),
        key=lambda e: (e.entry_day, e.entry_time),
    )[-400:]
    for idx, entry in enumerate(fee_entries):
        for k in range(FEE_SEGMENTS):
            entry.segments[k].amount = FEE_TARIFFS[(idx * FEE_SEGMENTS + k) % len(FEE_TARIFFS)]
    issues.append(
        {
            "rule_id": "B-02",
            "entity": FEE_ACCOUNT,
            "note": (
                f"Fee income account {FEE_ACCOUNT} is fed on the last {len(fee_entries)} "
                f"nights with tariff amounts only (1.49, 4.95, 49.90, ...), so its "
                f"first-two-digits distribution is far from Benford"
            ),
        }
    )

    # -- B-04: manual postings on weekends and public holidays ------------
    off_days = list(B04_HOLIDAYS) + list(WEEKEND_DAYS)
    pool = _free(ledger, skip_preparer=b01_clerk)
    for day, entry in zip(off_days, _take(rng, pool, len(off_days)), strict=True):
        entry.entry_day = day
        entry.entry_time = f"{8 + int(rng.integers(0, 9)):02d}:{int(rng.integers(0, 60)):02d}:00"
        entry.posting_day = day
        entry.frozen = True
        label = "public holiday" if is_holiday(day) else "weekend"
        issues.append(_issue("B-04", f"Manual entry entered on {day} ({label})", entry))

    # -- B-05: five post-closing entries (one combined with B-08) --------
    cross = cross_account_employee(bank, people)
    post_pool = [
        e
        for e in _free(ledger)
        if e.entry_day.day >= 12 and previous_period(e.entry_day) in ledger.close_periods
    ]
    posts = _take(rng, post_pool, 5)
    for idx, entry in enumerate(posts):
        previous = month_end(previous_period(entry.entry_day))
        entry.posting_day = last_workday_before(previous + timedelta(days=1))
        entry.frozen = True
        if idx == 0:
            if entry.total < MANUAL_LIMIT:
                entry.segments[0].amount += MANUAL_LIMIT - entry.total + 1
            entry.preparer = people.personal[cross]
            entry.approval = "self"
        issues.append(
            _issue(
                "B-05",
                f"Entry of {entry.entry_day} is posted into {entry.posting_day:%Y-%m} "
                f"after that period was closed",
                entry,
            )
        )
    # three early reversals of period-end revenue
    period_ends = [
        last_workday_before(month_end(f"2025-{month:02d}") + timedelta(days=1))
        for month in (2, 5, 8)
    ]
    for day, clerk in zip(period_ends, ledger.clerks[10:13], strict=True):
        original = _manual_entry(
            ledger,
            people,
            clerk,
            day,
            _invented_amount(rng) + 25_000,
            "1500",
            "4100",
            "Abgrenzung Zinsertraege zum Monatsende",
        )
        reversal_day = first_workdays(day, 3)[2]
        reversal = _manual_entry(
            ledger,
            people,
            clerk,
            reversal_day,
            original.segments[0].amount,
            "4100",
            "1500",
            "Storno Abgrenzung Zinsertraege",
        )
        reversal.reversal_of = original
        issues.append(
            _issue(
                "B-05",
                f"Entry of {day} (last days of the period) is reversed on "
                f"{reversal_day}, inside the first days of the next period",
                original,
            )
        )

    # -- B-07: five manual entries posted twice --------------------------
    singles = _free(ledger, single=True, skip_preparer=b01_clerk)
    for delay, original in enumerate(_take(rng, singles, 5), start=1):
        # a posting day inside the window and never on a weekend/holiday
        target = _shift_to_workday(original.posting_day, delay)
        clone = _clone_of(ledger, people, original, target)
        # the amount of both entries must stay identical, so neither may be
        # bumped by the duplicate resolution
        original.frozen = original.protected = True
        issues.append(
            _issue(
                "B-07",
                f"Same amount and lines as the entry of {original.entry_day}, posted "
                f"again {delay} day{'s' if delay > 1 else ''} later",
                clone,
            )
        )
    # near miss: an identical entry eight days later is outside the window
    late = singles[-1]
    if not late.frozen:
        _clone_of(ledger, people, late, _shift_to_workday(late.posting_day, 8), protected=True)

    # -- B-08: missing and self approval ---------------------------------
    for entry in _take(rng, [e for e in _free(ledger, minimum=MANUAL_LIMIT)], 3):
        entry.approval = "missing"
        entry.frozen = True
        issues.append(
            _issue(
                "B-08",
                f"Entry of {people.employee_of(entry.preparer)} above the approval limit "
                f"was never approved",
                entry,
            )
        )
    for _idx in range(2):
        entry = _take(rng, [e for e in _free(ledger, minimum=MANUAL_LIMIT)], 1)[0]
        entry.preparer = people.personal[cross]
        entry.approval = "self"
        entry.frozen = True
        issues.append(
            _issue(
                "B-08",
                f"Entry prepared and approved by {cross} through a second GL account of "
                f"the same employee",
                entry,
            )
        )
    issues.append(
        _issue(
            "B-08",
            f"Post-closing entry prepared and approved by {cross} "
            f"(the same entry as the first B-05 finding)",
            posts[0],
        )
    )

    # -- B-09: rare debit/credit pairs ------------------------------------
    plain = _free(ledger, single=True)
    for (debit, credit), entry in zip(RARE_PAIRS, _take(rng, plain, len(RARE_PAIRS)), strict=True):
        entry.segments[0] = Segment(debit, credit, entry.segments[0].amount)
        entry.frozen = True
        issues.append(
            _issue(
                "B-09",
                f"Manual entry debits {debit} ({ACCOUNT_CLASS[debit]}) and credits "
                f"{credit} ({ACCOUNT_CLASS[credit]}); this pair occurs in no other entry "
                f"of the year",
                entry,
            )
        )

    # -- B-10: preparers who do not usually post --------------------------
    admins = [
        emp
        for emp in sorted(people.personal)
        if people.org_unit[emp] == "Rechnungswesen"
        and people.job_function[emp] == "Administration"
        and people.eligible(emp)
    ]
    revision = [
        emp
        for emp in sorted(people.personal)
        if people.org_unit[emp] == "Revision" and people.eligible(emp)
    ]
    if len(admins) < 2 or len(revision) < 2:
        raise RuntimeError("not enough unusual preparers for B-10")
    unusual = [admins[0], admins[1], revision[0], revision[1]]
    for emp, entry in zip(unusual, _take(rng, _free(ledger), 4), strict=True):
        entry.preparer = people.personal[emp]
        entry.frozen = True
        issues.append(
            _issue(
                "B-10",
                f"Manual entry prepared by {emp} "
                f"({people.org_unit[emp]}/{people.job_function[emp]}), who does not "
                f"usually post journal entries",
                entry,
            )
        )

    assert near_miss_clerk
    return issues


def _manual_entry(
    ledger,  # noqa: ANN001
    people,  # noqa: ANN001
    employee: str,
    day,
    amount: int,
    debit: str,
    credit: str,
    description: str,
    *,
    approver: str = "",
) -> object:
    """A new manual entry at the end of the ledger (frozen, not protected)."""
    from audit_workbench.synth.ledger import Entry, Segment  # noqa: PLC0415

    entry = Entry(
        kind="manual",
        entry_day=day,
        entry_time="10:20:00",
        posting_day=day,
        preparer=people.personal[employee],
        segments=[Segment(debit, credit, amount)],
        description=description,
        frozen=True,
    )
    entry.approver = approver
    ledger.entries.append(entry)
    return entry


def _shift_to_workday(day, delay: int):
    """*day* plus *delay* days, moved to the next working day if necessary."""
    from audit_workbench.synth.ledger import JOURNAL_AS_OF, is_workday  # noqa: PLC0415

    candidate = day + timedelta(days=delay)
    for _ in range(7):
        if is_workday(candidate) and candidate <= JOURNAL_AS_OF:
            return candidate
        candidate += timedelta(days=1)
    raise RuntimeError(f"no working day after {day}")


def _clone_of(
    ledger,  # noqa: ANN001
    people,  # noqa: ANN001
    original,  # noqa: ANN001
    day,
    *,
    protected: bool = False,
) -> object:
    from audit_workbench.synth.ledger import Entry, Segment  # noqa: PLC0415

    clone = Entry(
        kind="manual",
        entry_day=day,
        entry_time=original.entry_time,
        posting_day=day,
        preparer=original.preparer,
        segments=[Segment(s.debit, s.credit, s.amount) for s in original.segments],
        description=original.description,
        protected=True,
        frozen=True,
    )
    ledger.entries.append(clone)
    assert people
    return clone


def resolve_journal_issues(issues: list[dict[str, object]]) -> list[dict[str, str]]:
    """Turn the entry references of the ground truth into the document numbers."""
    out: list[dict[str, str]] = []
    for issue in issues:
        entry = issue.get("entry")
        entity = str(issue.get("entity") or "")
        if entry is not None:
            entity = entry.doc_no  # type: ignore[attr-defined]
        out.append(
            {
                "rule_id": str(issue["rule_id"]),
                "entity": entity,
                "note": str(issue["note"]),
            }
        )
    return out
