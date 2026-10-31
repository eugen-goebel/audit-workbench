"""Access-review audit rules A-01 to A-08.

Eight rules that together cover the key risk areas of an access review:

* A-01  Leaver still has an active account.
* A-02  Account has not been used for an extended period.
* A-03  A shared (generic) account permits interactive login.
* A-04  Privileged access has not been recertified in the required window.
* A-05  One employee can perform two incompatible business functions (SoD).
* A-06  A recertification reviewer processed items without exercising care.
* A-07  A personal account has no matching HR employee record.
* A-08  An employee holds a role that almost none of their peers hold.

Each rule exposes a ``run(data: AccessData) -> list[Finding]`` method.
The module also exports ``ACCESS_RULES`` (a ``RuleRegistry``) and the
convenience function ``run_access_rules``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from audit_workbench.access.data import AccessData
from audit_workbench.access.sod import employee_functions, load_sod_ruleset
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.core.rules import Rule, RuleRegistry

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_privileged_safe(data: AccessData, role_id: str) -> bool:
    """Return whether *role_id* is privileged (directly or via hierarchy).

    Returns ``False`` silently for role ids not present in the role graph,
    treating unknown roles as holding nothing (spec edge-case rule).
    """
    if role_id not in data.role_graph:
        return False
    return data.is_privileged(role_id)


def _leaver_account_ids(data: AccessData) -> set[str]:
    """Return account ids of active personal accounts whose employee has left.

    These are the accounts that A-01 covers (regardless of grace_days); used
    by A-02 to exclude them so the stronger leaver finding takes precedence.
    """
    accts = data.accounts
    emps = data.employees
    personal_active = accts[(accts["account_type"] == "personal") & (accts["status"] == "active")]
    result: set[str] = set()
    for acct_id, row in personal_active.iterrows():
        emp_id = row["employee_id"]
        if emp_id and emp_id in emps.index and emps.at[emp_id, "has_left"]:
            result.add(acct_id)
    return result


# ---------------------------------------------------------------------------
# A-01  leaver_active
# ---------------------------------------------------------------------------


class LeaverActive(Rule):
    """A-01: Account still active after the employee's exit date.

    An employee who has left the organisation should have all personal accounts
    deactivated promptly. An active account after exit is both an access risk
    (the person no longer has a business need) and a segregation risk (they may
    retain capabilities to modify records or approve transactions).
    """

    rule_id: str = "A-01"
    version: str = "1.0.0"
    title: str = "Account active after employee exit"

    def __init__(self, *, grace_days: int = 0, **kwargs: Any) -> None:
        super().__init__(grace_days=grace_days, **kwargs)
        self.grace_days = grace_days

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        accts = data.accounts
        emps = data.employees
        ctx = {"as_of": data.as_of.isoformat()}

        population = accts[(accts["account_type"] == "personal") & (accts["status"] == "active")]

        findings: list[Finding] = []
        for acct_id, row in population.iterrows():
            emp_id = row["employee_id"]
            if not emp_id or emp_id not in emps.index:
                continue
            exit_date = emps.at[emp_id, "exit_date"]
            if exit_date is None:
                continue
            if (exit_date + timedelta(days=self.grace_days)) >= data.as_of:
                continue  # still within the grace window

            days_since = (data.as_of - exit_date).days
            last_login = row["last_login"]
            logged_in_after = not pd.isna(last_login) and last_login.date() > exit_date
            last_login_val = None if pd.isna(last_login) else last_login.isoformat()

            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=acct_id,
                    description=(
                        f"Account {acct_id} is active {days_since} days after "
                        f"the employee left on {exit_date}."
                    ),
                    evidence=data.hashes("accounts", "hr_employees"),
                    details={
                        "employee_id": emp_id,
                        "exit_date": str(exit_date),
                        "days_since_exit": days_since,
                        "last_login": last_login_val,
                        "logged_in_after_exit": logged_in_after,
                    },
                    context=ctx,
                )
            )

        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# A-02  dormant_account
# ---------------------------------------------------------------------------


class DormantAccount(Rule):
    """A-02: Active interactive account unused beyond the dormancy threshold.

    Dormant accounts represent a standing attack surface: credentials that are
    no longer monitored by their owner but still valid for authentication.
    Active leavers are excluded here because A-01 is the stronger finding.
    """

    rule_id: str = "A-02"
    version: str = "1.0.0"
    title: str = "Dormant interactive account"

    def __init__(self, *, dormant_days: int = 90, **kwargs: Any) -> None:
        super().__init__(dormant_days=dormant_days, **kwargs)
        self.dormant_days = dormant_days

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        accts = data.accounts
        ctx = {"as_of": data.as_of.isoformat()}

        population = accts[(accts["status"] == "active") & (accts["interactive"])]
        leaver_ids = _leaver_account_ids(data)
        cutoff = pd.Timestamp(data.as_of - timedelta(days=self.dormant_days))

        findings: list[Finding] = []
        for acct_id, row in population.iterrows():
            if acct_id in leaver_ids:
                continue  # covered by A-01

            last_login = row["last_login"]
            created_at = row["created_at"]

            if pd.isna(last_login):
                # Never logged in: dormant only if the account itself is old enough
                if pd.isna(created_at) or created_at >= cutoff:
                    continue
            elif last_login >= cutoff:
                continue  # used recently enough

            days_inactive = (
                (data.as_of - last_login.date()).days
                if not pd.isna(last_login)
                else (data.as_of - created_at.date()).days
            )
            last_login_val = None if pd.isna(last_login) else last_login.isoformat()

            findings.append(
                self.finding(
                    severity=Severity.MEDIUM,
                    entity=acct_id,
                    description=(
                        f"Account {acct_id} has been inactive for {days_inactive} days "
                        f"(threshold {self.dormant_days})."
                    ),
                    evidence=data.hashes("accounts", "hr_employees"),
                    details={"last_login": last_login_val, "days_inactive": days_inactive},
                    context=ctx,
                )
            )

        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# A-03  generic_interactive
# ---------------------------------------------------------------------------


class GenericInteractive(Rule):
    """A-03: Shared (generic) account that allows interactive login.

    Generic accounts are shared among multiple people, so individual actions
    cannot be attributed to a named user.  Permitting interactive login removes
    accountability and is incompatible with most audit trails.
    """

    rule_id: str = "A-03"
    version: str = "1.0.0"
    title: str = "Generic account with interactive login"

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        accts = data.accounts
        ctx = {"as_of": data.as_of.isoformat()}
        population = accts[
            (accts["account_type"] == "generic")
            & (accts["interactive"])
            & (accts["status"] == "active")
        ]

        findings: list[Finding] = [
            self.finding(
                severity=Severity.MEDIUM,
                entity=acct_id,
                description=(
                    f"Generic account {acct_id} on {row['system']} "
                    f"is active and permits interactive login."
                ),
                evidence=data.hashes("accounts"),
                context=ctx,
            )
            for acct_id, row in population.iterrows()
        ]
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# A-04  privileged_unreviewed
# ---------------------------------------------------------------------------


class PrivilegedUnreviewed(Rule):
    """A-04: Privileged access without a recertification decision in the window.

    Privileged roles carry elevated risk; regulators expect periodic review to
    confirm that the access is still needed.  An assignment that has not been
    reviewed within ``recert_days`` represents unattested standing privilege.
    """

    rule_id: str = "A-04"
    version: str = "1.0.0"
    title: str = "Privileged access not recertified"

    def __init__(self, *, recert_days: int = 365, **kwargs: Any) -> None:
        super().__init__(recert_days=recert_days, **kwargs)
        self.recert_days = recert_days

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        accts = data.accounts
        campaigns = data.campaigns
        decisions = data.decisions
        ctx = {"as_of": data.as_of.isoformat()}

        # Active accounts only
        active_accts = accts[accts["status"] == "active"]

        # Campaigns whose end_date falls in [as_of - recert_days, as_of]
        window_start = data.as_of - timedelta(days=self.recert_days)
        valid_camps = campaigns[
            (campaigns["end_date"] >= window_start) & (campaigns["end_date"] <= data.as_of)
        ]
        # campaign_id -> system
        camp_system: dict[str, str] = (
            valid_camps.set_index("campaign_id")["system"].to_dict()
            if not valid_camps.empty
            else {}
        )

        # Build set of (account_id, role_id) pairs reviewed in a valid campaign
        # matching the account's own system
        acct_system: dict[str, str] = active_accts["system"].to_dict()
        reviewed: set[tuple[str, str]] = set()
        if camp_system and not decisions.empty:
            valid_decisions = decisions[decisions["campaign_id"].isin(camp_system)]
            for _, drow in valid_decisions.iterrows():
                aid = drow["account_id"]
                cid = drow["campaign_id"]
                if aid in acct_system and cid in camp_system:
                    if acct_system[aid] == camp_system[cid]:
                        reviewed.add((aid, drow["role_id"]))

        # Cache is_privileged per role to avoid repeated graph traversal
        priv_cache: dict[str, bool] = {}

        def is_priv(role_id: str) -> bool:
            if role_id not in priv_cache:
                priv_cache[role_id] = _is_privileged_safe(data, role_id)
            return priv_cache[role_id]

        assignments = data.active_assignments
        pop = assignments[assignments["account_id"].isin(active_accts.index)]

        findings: list[Finding] = []
        for _, row in pop.iterrows():
            acct_id = row["account_id"]
            role_id = row["role_id"]
            if not is_priv(role_id):
                continue
            if (acct_id, role_id) in reviewed:
                continue

            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=f"{acct_id}:{role_id}",
                    description=(
                        f"Privileged assignment {acct_id}:{role_id} has no recertification "
                        f"decision within the last {self.recert_days} days."
                    ),
                    evidence=data.hashes(
                        "accounts",
                        "account_roles",
                        "roles",
                        "role_hierarchy",
                        "recert_campaigns",
                        "recert_decisions",
                    ),
                    context=ctx,
                )
            )

        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# A-05  sod_conflict
# ---------------------------------------------------------------------------


class SodConflictRule(Rule):
    """A-05: Employee holds two business functions that must be segregated.

    Segregation of duties prevents a single person from initiating and
    approving the same transaction (or performing other incompatible functions).
    Conflicts are detected across all accounts of an employee, because holding
    one function on account A and the conflicting function on account B is
    equally risky.
    """

    rule_id: str = "A-05"
    version: str = "1.0.0"
    title: str = "Segregation-of-duties conflict"

    def __init__(self, *, ruleset_path: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(ruleset_path=str(ruleset_path) if ruleset_path else None, **kwargs)
        self._ruleset_path = Path(ruleset_path) if ruleset_path else None

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        ruleset = load_sod_ruleset(self._ruleset_path)
        emp_fns = employee_functions(data)
        ctx = {"as_of": data.as_of.isoformat()}

        findings: list[Finding] = []
        for emp_id, fn_paths in sorted(emp_fns.items()):
            held_fns = set(fn_paths)
            for conflict in ruleset:
                fn_a, fn_b = conflict.functions
                if fn_a not in held_fns or fn_b not in held_fns:
                    continue

                findings.append(
                    self.finding(
                        severity=conflict.severity,
                        entity=f"{emp_id}:{conflict.id}",
                        description=(
                            f"Employee {emp_id} holds both {fn_a} and {fn_b} "
                            f"({conflict.description})."
                        ),
                        evidence=data.hashes(
                            "accounts",
                            "account_roles",
                            "roles",
                            "role_hierarchy",
                            "entitlements",
                            "role_entitlements",
                        ),
                        details={fn_a: fn_paths[fn_a], fn_b: fn_paths[fn_b]},
                        context=ctx,
                    )
                )

        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# A-06  recert_rubber_stamp
# ---------------------------------------------------------------------------


class RecertRubberStamp(Rule):
    """A-06: Recertification reviewer processed items without exercising care.

    Two indicators of careless review are detected:
    - **Self-review**: the reviewer approved access for their own account.
    - **Speed**: the reviewer processed too many items too quickly to have
      read and understood each one (fewer than ``fast_seconds`` per item on
      at least ``fast_share`` of a batch of ``min_items`` or more).
    """

    rule_id: str = "A-06"
    version: str = "1.0.0"
    title: str = "Recertification rubber stamp"

    def __init__(
        self,
        *,
        min_items: int = 20,
        fast_seconds: float = 5.0,
        fast_share: float = 0.8,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            min_items=min_items, fast_seconds=fast_seconds, fast_share=fast_share, **kwargs
        )
        self.min_items = min_items
        self.fast_seconds = fast_seconds
        self.fast_share = fast_share

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        decisions = data.decisions
        accts = data.accounts
        ctx = {"as_of": data.as_of.isoformat()}

        if decisions.empty:
            return []

        findings: list[Finding] = []

        for (campaign_id, reviewer_emp_id), group in decisions.groupby(
            ["campaign_id", "reviewer_employee_id"]
        ):
            total = len(group)

            # Count fast decisions
            fast_count = 0
            for _, row in group.iterrows():
                if pd.isna(row["item_shown_at"]) or pd.isna(row["decided_at"]):
                    continue
                duration = (row["decided_at"] - row["item_shown_at"]).total_seconds()
                if duration < self.fast_seconds:
                    fast_count += 1

            # Find self-reviewed accounts (reviewer 'keep'-decided their own account)
            self_reviewed: list[str] = []
            for _, row in group.iterrows():
                acct_id = row["account_id"]
                if acct_id not in accts.index:
                    continue
                if (
                    accts.at[acct_id, "employee_id"] == reviewer_emp_id
                    and row["decision"] == "keep"
                ):
                    self_reviewed.append(acct_id)

            triggers: list[str] = []
            if self_reviewed:
                triggers.append("self_review")
            if total >= self.min_items and total > 0 and (fast_count / total) >= self.fast_share:
                triggers.append("speed")

            if not triggers:
                continue

            severity = Severity.HIGH if "self_review" in triggers else Severity.MEDIUM

            findings.append(
                self.finding(
                    severity=severity,
                    entity=f"{campaign_id}:{reviewer_emp_id}",
                    description=(
                        f"Reviewer {reviewer_emp_id} in campaign {campaign_id} "
                        f"triggered: {', '.join(triggers)}."
                    ),
                    evidence=data.hashes("accounts", "recert_campaigns", "recert_decisions"),
                    details={
                        "triggers": triggers,
                        "decisions": total,
                        "fast_decisions": fast_count,
                        "self_reviewed_accounts": sorted(set(self_reviewed)),
                    },
                    context=ctx,
                )
            )

        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# A-07  orphan_account
# ---------------------------------------------------------------------------


class OrphanAccount(Rule):
    """A-07: Personal account with no corresponding HR employee record.

    A personal account is supposed to be bound to exactly one named employee.
    If that employee cannot be found in HR the account has no legitimate owner:
    access rights cannot be reviewed and the account may have been created
    fraudulently or left behind after an incomplete off-boarding.
    """

    rule_id: str = "A-07"
    version: str = "1.0.0"
    title: str = "Orphan personal account"

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        accts = data.accounts
        emps = data.employees
        ctx = {"as_of": data.as_of.isoformat()}

        population = accts[(accts["account_type"] == "personal") & (accts["status"] == "active")]

        findings: list[Finding] = [
            self.finding(
                severity=Severity.HIGH,
                entity=acct_id,
                description=(
                    f"Personal account {acct_id} has no matching HR employee record "
                    f"(employee_id={row['employee_id']!r})."
                ),
                evidence=data.hashes("accounts", "hr_employees"),
                context=ctx,
            )
            for acct_id, row in population.iterrows()
            if not row["employee_id"] or row["employee_id"] not in emps.index
        ]
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# A-08  unusual_role
# ---------------------------------------------------------------------------


class UnusualRole(Rule):
    """A-08: An employee holds a role that almost none of their peers hold.

    Peer groups are defined by (org_unit, job_function) for employees who have
    not left. A role held by fewer than ``rare_share`` of a peer group is
    unusual and warrants investigation: it may indicate excessive access,
    role misconfiguration, or a legitimate but undocumented business need.

    The peer group is every employee in HR who has not left and shares the
    org unit and job function, including people without any account. The
    assessed person is part of the group and counts as a holder.
    """

    rule_id: str = "A-08"
    version: str = "1.0.0"
    title: str = "Unusual role for peer group"

    def __init__(self, *, min_peers: int = 5, rare_share: float = 0.1, **kwargs: Any) -> None:
        super().__init__(min_peers=min_peers, rare_share=rare_share, **kwargs)
        self.min_peers = min_peers
        self.rare_share = rare_share

    def run(self, data: AccessData) -> list[Finding]:  # type: ignore[override]
        accts = data.accounts
        emps = data.employees
        assignments = data.active_assignments
        ctx = {"as_of": data.as_of.isoformat()}

        # Active personal accounts of employees who have not left
        # Use == False to avoid issues with object-dtype bool mask on empty frames
        not_left_ids = set(emps.index[emps["has_left"] == False])  # noqa: E712
        active_personal = accts[
            (accts["account_type"] == "personal")
            & (accts["status"] == "active")
            & (accts["employee_id"].isin(not_left_ids))
        ]
        active_personal_ids = set(active_personal.index)

        # Population: active assignments for those accounts
        pop = assignments[assignments["account_id"].isin(active_personal_ids)]

        if pop.empty:
            return []

        # account_id -> employee_id
        acct_emp: dict[str, str] = active_personal["employee_id"].to_dict()

        # Peer groups come from HR: every employee who has not left, whether or
        # not they have an account. Counting only account holders would make a
        # role look common in a team where most people have no access at all.
        stayers = emps.loc[sorted(not_left_ids)]
        emp_peer_key: dict[str, tuple[str, str]] = {
            eid: (org, fn)
            for eid, org, fn in zip(
                stayers["employee_id"], stayers["org_unit"], stayers["job_function"], strict=True
            )
        }
        peer_groups: dict[tuple[str, str], set[str]] = {}
        for emp_id, key in emp_peer_key.items():
            peer_groups.setdefault(key, set()).add(emp_id)

        # (employee_id, role_id) -> True if held on any active personal account
        emp_role_held: set[tuple[str, str]] = set()
        for _, row in pop.iterrows():
            eid = acct_emp.get(row["account_id"], "")
            if eid:
                emp_role_held.add((eid, row["role_id"]))

        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()

        for _, row in pop.iterrows():
            acct_id = row["account_id"]
            role_id = row["role_id"]
            key = (acct_id, role_id)
            if key in seen:
                continue
            seen.add(key)

            emp_id = acct_emp.get(acct_id, "")
            if not emp_id or emp_id not in emp_peer_key:
                continue

            peer_key = emp_peer_key[emp_id]
            peer_group = peer_groups.get(peer_key, set())
            pg_size = len(peer_group)

            if pg_size < self.min_peers:
                continue

            peers_with_role = sum(1 for p in peer_group if (p, role_id) in emp_role_held)

            if peers_with_role / pg_size >= self.rare_share:
                continue

            findings.append(
                self.finding(
                    severity=Severity.LOW,
                    entity=f"{acct_id}:{role_id}",
                    description=(
                        f"Account {acct_id} holds role {role_id}, "
                        f"which only {peers_with_role}/{pg_size} peers hold "
                        f"(threshold {self.rare_share:.0%})."
                    ),
                    evidence=data.hashes("accounts", "account_roles", "hr_employees"),
                    details={"peer_group_size": pg_size, "peers_with_role": peers_with_role},
                    context=ctx,
                )
            )

        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# Registry and convenience function
# ---------------------------------------------------------------------------

ACCESS_RULES = RuleRegistry()
for _cls in [
    LeaverActive,
    DormantAccount,
    GenericInteractive,
    PrivilegedUnreviewed,
    SodConflictRule,
    RecertRubberStamp,
    OrphanAccount,
    UnusualRole,
]:
    ACCESS_RULES.register(_cls)


def run_access_rules(
    data: AccessData,
    params: dict[str, dict[str, Any]] | None = None,
) -> list[Finding]:
    """Run all eight access rules on *data* and return the combined findings."""
    return ACCESS_RULES.run_all(data, params)
