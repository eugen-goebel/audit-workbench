"""Cross-module audit rules X-01 to X-03 (spec docs/design/phase-4-crosslink.md, sections 1 and 3).

Modules A, B and C each test one data set. A finding in module A is a risk:
someone could do something they should not. The X rules ask whether the risk
was *used*, by joining the action data of the journal (B) and the change data
(C) with the rights of the access data (A) **at the day of the action**:

* X-01  One employee held GL_POST and GL_APPROVE and used both on one entry.
* X-02  A journal entry was prepared or approved by an account that did not
        hold the required function on that day.
* X-03  A leaver acted after the exit date.

The rules read the three data sets through their existing loaders and report
the hashes of every file they used. ``details.related`` lists the
``(rule_id, entity)`` keys of A, B and C findings about the same objects; it is
computed from the findings passed to `run_crosslink_rules`, never from the
ground truth. What counts as "the same objects" is defined per rule below.

Each rule exposes ``run(data: CrosslinkData) -> list[Finding]``. The module also
exports ``CROSSLINK_RULES`` (a ``RuleRegistry``) and ``run_crosslink_rules``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from audit_workbench.access.data import AccessData
from audit_workbench.access.sod import employee_functions, load_sod_ruleset
from audit_workbench.change.data import ChangeData
from audit_workbench.core.evidence import EvidenceError
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.core.rules import Rule, RuleRegistry
from audit_workbench.journal.data import JournalData

JE_PREPARE = "je_prepare"
JE_APPROVE = "je_approve"

ACTION_FUNCTION: dict[str, str] = {JE_PREPARE: "GL_POST", JE_APPROVE: "GL_APPROVE"}
"""Action types that need a function (spec section 1). Deployments are not tested (D1)."""

_FUNCTION_ACTION = {fn: action for action, fn in ACTION_FUNCTION.items()}

# Access files the "holds function F at day d" check reads.
_RIGHTS_FILES = (
    "accounts",
    "account_roles",
    "roles",
    "role_hierarchy",
    "entitlements",
    "role_entitlements",
)


@dataclass(frozen=True)
class CrosslinkData:
    """The three loaded data sets plus the A, B and C findings used for `details.related`."""

    access: AccessData
    journal: JournalData
    change: ChangeData
    related: tuple[Finding, ...] = ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(data: CrosslinkData) -> dict[str, str]:
    return {"as_of": data.access.as_of.isoformat()}


def _evidence(data: CrosslinkData, *, access=(), journal=(), change=()) -> list[str]:
    """Hashes of the named files, in a fixed order, each hash once."""
    hashes = [
        *data.access.hashes(*access),
        *data.journal.hashes(*journal),
        *data.change.hashes(*change),
    ]
    return list(dict.fromkeys(hashes))


def _py(ts: Any) -> datetime:
    """A pandas `Timestamp` or `datetime` as plain `datetime`."""
    return ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts


def _money(cents: int) -> str:
    return str(Decimal(int(cents)).scaleb(-2))


def _pairs(findings: Iterable[Finding]) -> list[list[str]]:
    """`details.related`: unique ``[rule_id, entity]`` pairs, sorted."""
    return [list(p) for p in sorted({(f.rule_id, f.entity) for f in findings})]


def _journal_ts(value: Any, doc_no: str, column: str) -> datetime:
    if pd.isna(value):
        raise EvidenceError(f"journal_entries.{column}: empty timestamp for {doc_no}")
    return _py(value)


class _HeldAt:
    """`AccessData.holds_function` with a cache; the same triples recur for many entries."""

    def __init__(self, access: AccessData) -> None:
        self._access = access
        self._cache: dict[tuple[str, str, date], bool] = {}

    def __call__(self, account_id: str, function: str, day: date) -> bool:
        key = (account_id, function, day)
        if key not in self._cache:
            self._cache[key] = self._access.holds_function(account_id, function, day)
        return self._cache[key]


def _why_not_held(access: AccessData, account_id: str, day: date) -> str:
    """Why an account does not hold a function on *day*: the three possible reasons."""
    if account_id not in access.accounts.index:
        return "account_unknown"
    created = access.accounts.at[account_id, "created_at"]
    if pd.isna(created) or created.date() > day:
        return "account_not_created"
    return "function_not_held"


# ---------------------------------------------------------------------------
# X-01  sod_exercised
# ---------------------------------------------------------------------------


class SodExercised(Rule):
    """X-01: One employee held two conflicting functions and used both on one entry.

    A-05 reports that someone *can* post and approve. X-01 reports that
    someone *did*: a manual entry whose preparer and approver account belong to
    the same employee, where the preparer account held GL_POST on the entry
    day and the approver account held GL_APPROVE on the approval day. Unlike
    B-08 it also covers entries below the approval limit.

    Only conflicts whose two functions are both in the action table are in
    scope (today SOD-GL-01). Related findings: A-05 with the same entity and
    B-08 findings of the entries in `doc_nos`.
    """

    rule_id = "X-01"
    version = "1.0.0"
    title = "Conflicting functions held and used on one transaction"

    def __init__(self, *, ruleset_path: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(ruleset_path=str(ruleset_path) if ruleset_path else None, **kwargs)
        self._ruleset_path = Path(ruleset_path) if ruleset_path else None

    def run(self, data: CrosslinkData) -> list[Finding]:  # type: ignore[override]
        access, journal = data.access, data.journal
        conflicts = [
            c
            for c in load_sod_ruleset(self._ruleset_path)
            if all(fn in _FUNCTION_ACTION for fn in c.functions)
            and len({_FUNCTION_ACTION[fn] for fn in c.functions}) == 2
        ]
        if not conflicts:
            return []

        held = _HeldAt(access)
        limit = journal.manual_limit_cents
        entries = journal.manual_entries
        entries = entries[entries["approver_account_id"] != ""]

        # (employee, conflict id) -> exercised entries
        groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in entries.itertuples(index=False):
            employee = row.preparer_employee_id
            if employee is None or employee != row.approver_employee_id:
                continue
            entry_ts = _journal_ts(row.entry_ts, row.doc_no, "entry_ts")
            approved_ts = _journal_ts(row.approved_ts, row.doc_no, "approved_ts")
            for conflict in conflicts:
                prepare_fn = next(
                    f for f in conflict.functions if _FUNCTION_ACTION[f] == JE_PREPARE
                )
                approve_fn = next(
                    f for f in conflict.functions if _FUNCTION_ACTION[f] == JE_APPROVE
                )
                if held(row.preparer_account_id, prepare_fn, entry_ts.date()) and held(
                    row.approver_account_id, approve_fn, approved_ts.date()
                ):
                    groups[(employee, conflict.id)].append((row, entry_ts))

        by_id = {c.id: c for c in conflicts}
        held_now = employee_functions(access) if groups else {}
        findings: list[Finding] = []
        for (employee, conflict_id), items in groups.items():
            conflict = by_id[conflict_id]
            doc_nos = sorted(row.doc_no for row, _ in items)
            max_cents = max(int(row.amount_cents) for row, _ in items)
            first_ts = min(ts for _, ts in items)
            last_ts = max(ts for _, ts in items)
            severity = Severity.CRITICAL if max_cents >= limit else conflict.severity
            related = [
                f
                for f in data.related
                if (f.rule_id == "A-05" and f.entity == f"{employee}:{conflict_id}")
                or (f.rule_id == "B-08" and f.entity in doc_nos)
            ]
            fn_a, fn_b = conflict.functions
            findings.append(
                self.finding(
                    severity=severity,
                    entity=f"{employee}:{conflict_id}",
                    description=(
                        f"Employee {employee} held {fn_a} and {fn_b} ({conflict.description}) "
                        f"and used both on {len(doc_nos)} manual journal "
                        f"{'entry' if len(doc_nos) == 1 else 'entries'}: prepared and approved "
                        f"the same entry (largest {_money(max_cents)}, "
                        f"approval limit {_money(limit)})."
                    ),
                    evidence=_evidence(
                        data,
                        access=_RIGHTS_FILES,
                        journal=("journal_entries", "journal_lines", "approval_limits", "accounts"),
                    ),
                    details={
                        "doc_nos": doc_nos,
                        "count": len(doc_nos),
                        "first_ts": first_ts.isoformat(),
                        "last_ts": last_ts.isoformat(),
                        "max_amount": _money(max_cents),
                        "via_second_account": any(
                            row.preparer_account_id != row.approver_account_id for row, _ in items
                        ),
                        "held_on_as_of": all(
                            fn in held_now.get(employee, {}) for fn in (fn_a, fn_b)
                        ),
                        "related": _pairs(related),
                    },
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# X-02  action_without_entitlement
# ---------------------------------------------------------------------------


class ActionWithoutEntitlement(Rule):
    """X-02: A journal entry was prepared or approved without the right to do so.

    The acting account did not hold the required function (GL_POST to prepare,
    GL_APPROVE to approve) on the action day, or did not exist yet. Either the
    application does not enforce its authorisation model, or the access
    extract the audit relies on is incomplete. Both undermine every A result;
    the finding says so and does not guess which one applies.

    Related findings: every B finding about the same `doc_no` (entity is the
    doc_no, or a B-06 duplicate entity ending in ``:dup:<doc_no>``) and every A
    finding on the acting account of a flagged action (entity is the account
    id, or starts with ``<account id>:``).
    """

    rule_id = "X-02"
    version = "1.0.0"
    title = "Action not covered by the access data"

    def run(self, data: CrosslinkData) -> list[Finding]:  # type: ignore[override]
        access = data.access
        held = _HeldAt(access)

        findings: list[Finding] = []
        for row in data.journal.unique_entries.itertuples(index=False):
            checks: list[dict[str, str]] = []
            tests = []
            if row.source == "manual":
                tests.append((JE_PREPARE, row.preparer_account_id, "entry_ts", row.entry_ts))
            if row.approver_account_id != "":
                tests.append((JE_APPROVE, row.approver_account_id, "approved_ts", row.approved_ts))
            for action, account_id, column, value in tests:
                ts = _journal_ts(value, row.doc_no, column)
                function = ACTION_FUNCTION[action]
                if held(account_id, function, ts.date()):
                    continue
                checks.append(
                    {
                        "action": action,
                        "account_id": account_id,
                        "function": function,
                        "ts": ts.isoformat(),
                        "reason": _why_not_held(access, account_id, ts.date()),
                    }
                )
            if not checks:
                continue

            accounts = {c["account_id"] for c in checks}
            related = [
                f
                for f in data.related
                if (
                    f.rule_id.startswith("B-")
                    and (f.entity == row.doc_no or f.entity.endswith(f":dup:{row.doc_no}"))
                )
                or (
                    f.rule_id.startswith("A-")
                    and any(f.entity == a or f.entity.startswith(f"{a}:") for a in accounts)
                )
            ]
            what = "; ".join(
                f"{c['action']} by {c['account_id']} on {c['ts'][:10]} "
                f"without {c['function']} ({c['reason']})"
                for c in checks
            )
            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=row.doc_no,
                    description=(
                        f"Entry {row.doc_no}: {what}. Either the application does not enforce "
                        "its authorisation model, or the access extract is incomplete. Both "
                        "undermine the access results; this finding does not decide which."
                    ),
                    evidence=_evidence(
                        data,
                        access=_RIGHTS_FILES,
                        journal=("journal_entries", "accounts"),
                    ),
                    details={
                        "actions": [c["action"] for c in checks],
                        "checks": checks,
                        "related": _pairs(related),
                    },
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# X-03  activity_after_exit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Action:
    type: str
    id: str
    employee_id: str
    ts: datetime


def _actions(data: CrosslinkData) -> Iterable[_Action]:
    """Every action of the action table (spec section 1) that has an employee."""
    # je_prepare, je_approve
    for row in data.journal.unique_entries.itertuples(index=False):
        if row.source == "manual" and row.preparer_employee_id is not None:
            yield _Action(
                JE_PREPARE,
                row.doc_no,
                row.preparer_employee_id,
                _journal_ts(row.entry_ts, row.doc_no, "entry_ts"),
            )
        if row.approver_account_id != "" and row.approver_employee_id is not None:
            yield _Action(
                JE_APPROVE,
                row.doc_no,
                row.approver_employee_id,
                _journal_ts(row.approved_ts, row.doc_no, "approved_ts"),
            )
    # prod_deploy: the deploying employee of module C, section 2
    deployments = data.change.deployments
    for dep in deployments[deployments["environment"] == "prod"].itertuples(index=False):
        if dep.deploying_employee_id is not None:
            yield _Action(
                "prod_deploy", dep.deployment_id, dep.deploying_employee_id, dep.deployed_ts
            )
    # commit
    for commit in data.change.commits.itertuples(index=False):
        yield _Action("commit", commit.commit_sha, commit.author_employee_id, commit.committed_ts)
    # change_approval: every decision, `pre` and `post`, approved or rejected
    for approvals in data.change.approvals.values():
        for a in approvals:
            yield _Action("change_approval", a.change_id, a.approver_employee_id, a.decided_ts)
    # recert_decision
    for d in data.access.decisions.itertuples(index=False):
        if not pd.isna(d.decided_at):
            yield _Action(
                "recert_decision",
                f"{d.campaign_id}:{d.account_id}:{d.role_id}",
                d.reviewer_employee_id,
                _py(d.decided_at),
            )


class ActivityAfterExit(Rule):
    """X-03: A leaver acted after the exit date.

    A-01 reports the open door (the account is still active), X-03 reports
    that someone walked through it. An action on the exit date itself is not
    flagged. One finding per employee, with all actions after the exit date.

    Related findings: A-01 findings on accounts of the employee.
    """

    rule_id = "X-03"
    version = "1.0.0"
    title = "Activity after the exit date"

    def run(self, data: CrosslinkData) -> list[Finding]:  # type: ignore[override]
        access = data.access
        exits: dict[str, date] = {
            emp: exit_date
            for emp, exit_date in access.employees["exit_date"].items()
            if exit_date is not None
        }
        if not exits:
            return []

        after: dict[str, list[_Action]] = defaultdict(list)
        for action in _actions(data):
            exit_date = exits.get(action.employee_id)
            if exit_date is not None and action.ts.date() > exit_date:
                after[action.employee_id].append(action)

        accounts = access.accounts
        findings: list[Finding] = []
        for employee, items in after.items():
            exit_date = exits[employee]
            items.sort(key=lambda a: (a.ts, a.type, a.id))
            days_max = max((a.ts.date() - exit_date).days for a in items)
            own_accounts = set(accounts.index[accounts["employee_id"] == employee])
            related = [f for f in data.related if f.rule_id == "A-01" and f.entity in own_accounts]
            kinds = sorted({a.type for a in items})
            findings.append(
                self.finding(
                    severity=Severity.CRITICAL,
                    entity=employee,
                    description=(
                        f"Employee {employee} left on {exit_date} but acted {len(items)} "
                        f"time(s) afterwards ({', '.join(kinds)}), the last one {days_max} "
                        "day(s) after the exit date."
                    ),
                    evidence=_evidence(
                        data,
                        access=("hr_employees", "accounts", "recert_decisions"),
                        journal=("journal_entries", "accounts"),
                        change=("commits", "deployments", "change_approvals", "accounts"),
                    ),
                    details={
                        "exit_date": exit_date.isoformat(),
                        "actions": [
                            {"type": a.type, "id": a.id, "ts": a.ts.isoformat()} for a in items
                        ],
                        "days_after_exit_max": days_max,
                        "related": _pairs(related),
                    },
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CROSSLINK_RULES = RuleRegistry()
_RULES: tuple[type[Rule], ...] = (SodExercised, ActionWithoutEntitlement, ActivityAfterExit)
for _cls in _RULES:
    CROSSLINK_RULES.register(_cls)


def run_crosslink_rules(
    access: AccessData,
    journal: JournalData,
    change: ChangeData,
    related: Iterable[Finding] | None = None,
    params: dict[str, dict[str, Any]] | None = None,
) -> list[Finding]:
    """Run X-01 to X-03 and return the findings sorted by rule and entity.

    `related` are the A, B and C findings already computed on the same data;
    they fill `details.related`. `params` maps a rule id to its parameters,
    e.g. ``{"X-01": {"ruleset_path": "my_sod.yaml"}}``. Unknown ids raise `KeyError`.
    """
    params = params or {}
    unknown = sorted(set(params) - set(CROSSLINK_RULES.ids()))
    if unknown:
        raise KeyError(f"unknown crosslink rule id(s): {', '.join(unknown)}")
    data = CrosslinkData(access, journal, change, tuple(related or ()))
    findings = CROSSLINK_RULES.run_all(data, params)  # type: ignore[arg-type]
    return sorted(findings, key=lambda f: (f.rule_id, f.entity))
