"""Change management audit rules C-01 to C-06 (spec docs/design/module-c-change.md, section 3).

* C-01  Production deployment without an approved change.
* C-02  The developer of a change deployed it themselves.
* C-03  An emergency change was not approved afterwards (or too late).
* C-04  The approval of a normal change came after its first deployment.
* C-05  The approver requested or built the change.
* C-06  A normal medium/high risk change was deployed without a valid test sign-off.

Every rule works on the population of spec section 1 (production deployments
in the audit period and their changes), which `ChangeData` provides.
Each rule exposes ``run(data: ChangeData) -> list[Finding]``. The module also
exports ``CHANGE_RULES`` (a ``RuleRegistry``) and ``run_change_rules``.
"""

from __future__ import annotations

from typing import Any

from audit_workbench.change.data import ChangeData
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.core.rules import Rule, RuleRegistry

DORA_9_4_E = "DORA (EU) 2022/2554 Art. 9(4)(e): changes recorded, tested, approved"
RTS_17_1_B = (
    "RTS (EU) 2024/1774 Art. 17(1)(b): independence of approval from request and implementation"
)
RTS_17_1_C_III = (
    "RTS (EU) 2024/1774 Art. 17(1)(c)(iii): changes tested and finalised in a controlled manner"
)
RTS_17_1_F = "RTS (EU) 2024/1774 Art. 17(1)(f): emergency changes with adequate safeguards"
RTS_17_1_G = "RTS (EU) 2024/1774 Art. 17(1)(g): emergency changes approved after implementation"


def _ctx(data: ChangeData) -> dict[str, str]:
    return {"as_of": data.as_of.isoformat()}


def _iso(ts: Any) -> str:
    return ts.isoformat()


# ---------------------------------------------------------------------------
# C-01  deploy_without_change
# ---------------------------------------------------------------------------


class DeployWithoutChange(Rule):
    """C-01: Production deployment without an approved change.

    Triggers: no change id (`missing`), a change id that is not in the change
    file (`unknown`), a change of another system (`system_mismatch`), a normal
    change without effective approval or a cancelled change (`not_approved`).
    """

    rule_id = "C-01"
    version = "1.0.0"
    title = "Production deployment without an approved change"

    def run(self, data: ChangeData) -> list[Finding]:  # type: ignore[override]
        changes = data.changes
        findings: list[Finding] = []
        for dep in data.prod_deployments:
            triggers: set[str] = set()
            if not dep.change_id:
                triggers.add("missing")
            elif dep.change_id not in changes.index:
                triggers.add("unknown")
            else:
                change = changes.loc[dep.change_id]
                if change["system"] != dep.system:
                    triggers.add("system_mismatch")
                if change["status"] == "cancelled" or (
                    change["change_type"] == "normal"
                    and data.effective_approval(dep.change_id) is None
                ):
                    triggers.add("not_approved")
            if not triggers:
                continue
            ordered = sorted(triggers)
            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=dep.deployment_id,
                    description=(
                        f"Production deployment {dep.deployment_id} on {_iso(dep.deployed_ts)} "
                        f"has no approved change ({', '.join(ordered)})."
                    ),
                    evidence=data.hashes("deployments", "changes", "change_approvals", "accounts"),
                    details={
                        "triggers": ordered,
                        "deploying_employee_id": dep.deploying_employee_id,
                        "change_id": dep.change_id or None,
                    },
                    regulation_refs=[DORA_9_4_E],
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# C-02  self_deploy
# ---------------------------------------------------------------------------


class SelfDeploy(Rule):
    """C-02: The developer deployed their own change.

    The deploying employee (account owner for manual deployments, releasing
    person for pipeline deployments) authored a commit of the same change.
    """

    rule_id = "C-02"
    version = "1.0.0"
    title = "Developer deployed their own change"

    def run(self, data: ChangeData) -> list[Finding]:  # type: ignore[override]
        findings: list[Finding] = []
        for dep in data.prod_deployments:
            employee = dep.deploying_employee_id
            if not dep.change_id or employee is None:
                continue
            if employee not in data.commit_authors.get(dep.change_id, frozenset()):
                continue
            shas = data.commits_of(dep.change_id, author=employee)
            via_second = dep.method == "manual" and dep.account_id != data.primary_ad_account.get(
                employee
            )
            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=dep.deployment_id,
                    description=(
                        f"{employee} deployed change {dep.change_id} to production "
                        f"({dep.method}, account {dep.account_id}) and authored "
                        f"{len(shas)} of its commits."
                    ),
                    evidence=data.hashes("deployments", "commits", "accounts"),
                    details={
                        "deploying_employee_id": employee,
                        "account_id": dep.account_id,
                        "method": dep.method,
                        "commit_shas": shas,
                        "via_second_account": via_second,
                    },
                    regulation_refs=[RTS_17_1_B],
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# C-03  emergency_post_approval
# ---------------------------------------------------------------------------


class EmergencyPostApproval(Rule):
    """C-03: Emergency change without (timely) approval after implementation.

    The RTS sets no deadline; `post_days` (calendar days, default 7) is the
    auditor's parameter and is stated in every finding. Days are counted
    between calendar dates, so approval on day `post_days` is still in time.
    A change whose post-approval is still open at `as_of` is flagged only once
    more than `post_days` days have passed since its first deployment.
    """

    rule_id = "C-03"
    version = "1.0.0"
    title = "Emergency change not approved afterwards"

    def __init__(self, *, post_days: int = 7, **kwargs: Any) -> None:
        super().__init__(post_days=post_days, **kwargs)
        self.post_days = post_days

    def run(self, data: ChangeData) -> list[Finding]:  # type: ignore[override]
        findings: list[Finding] = []
        for cid in data.population:
            if data.changes.at[cid, "change_type"] != "emergency":
                continue
            first = data.first_prod_deployment(cid)
            assert first is not None  # population guarantees a deployment
            post = data.first_post_approval(cid)
            if post is None:
                waited = (data.as_of - first.deployed_ts.date()).days
                if waited <= self.post_days:
                    continue  # still within the deadline
                trigger, severity = "missing", Severity.HIGH
                days = None
                text = (
                    f"Emergency change {cid} was deployed on {_iso(first.deployed_ts)} "
                    f"and has no post-approval {waited} days later "
                    f"(allowed: {self.post_days} days)."
                )
            else:
                days = (post.decided_ts.date() - first.deployed_ts.date()).days
                if days <= self.post_days:
                    continue
                trigger, severity = "late", Severity.MEDIUM
                text = (
                    f"Emergency change {cid} was deployed on {_iso(first.deployed_ts)} "
                    f"and post-approved {days} days later (allowed: {self.post_days} days)."
                )
            findings.append(
                self.finding(
                    severity=severity,
                    entity=cid,
                    description=text,
                    evidence=data.hashes("changes", "change_approvals", "deployments"),
                    details={
                        "trigger": trigger,
                        "post_days": self.post_days,
                        "first_deployment_id": first.deployment_id,
                        "deployed_ts": _iso(first.deployed_ts),
                        "approved_ts": _iso(post.decided_ts) if post else None,
                        "days_after_deployment": days,
                    },
                    regulation_refs=[RTS_17_1_F, RTS_17_1_G],
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# C-04  approval_after_deploy
# ---------------------------------------------------------------------------


class ApprovalAfterDeploy(Rule):
    """C-04: The effective approval of a normal change is later than its first deployment."""

    rule_id = "C-04"
    version = "1.0.0"
    title = "Approval came after the deployment"

    def run(self, data: ChangeData) -> list[Finding]:  # type: ignore[override]
        findings: list[Finding] = []
        for cid in data.population:
            if data.changes.at[cid, "change_type"] != "normal":
                continue
            approval = data.effective_approval(cid)
            first = data.first_prod_deployment(cid)
            assert first is not None
            # Flag only if no approval was in force when production was first touched.
            # A re-approval after a deployment that was already approved is not late.
            if approval is None or data.approval_at(cid, first.deployed_ts) is not None:
                continue
            hours = round((approval.decided_ts - first.deployed_ts).total_seconds() / 3600, 2)
            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=cid,
                    description=(
                        f"Change {cid} was first deployed on {_iso(first.deployed_ts)}, "
                        f"{hours} hours before its approval on {_iso(approval.decided_ts)}."
                    ),
                    evidence=data.hashes("changes", "change_approvals", "deployments"),
                    details={
                        "first_deployment_id": first.deployment_id,
                        "deployed_ts": _iso(first.deployed_ts),
                        "approved_ts": _iso(approval.decided_ts),
                        "hours_before_approval": hours,
                    },
                    regulation_refs=[DORA_9_4_E],
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# C-05  approver_not_independent
# ---------------------------------------------------------------------------


class ApproverNotIndependent(Rule):
    """C-05: The approver requested or built the change they approved.

    One finding per change. If several approvals are affected, the details
    describe the earliest one.
    """

    rule_id = "C-05"
    version = "1.0.0"
    title = "Approver is not independent of the change"

    def run(self, data: ChangeData) -> list[Finding]:  # type: ignore[override]
        findings: list[Finding] = []
        for cid in data.population:
            requester = data.changes.at[cid, "requester_employee_id"]
            authors = data.commit_authors.get(cid, frozenset())
            for approval in data.approvals.get(cid, []):
                if approval.decision != "approved":
                    continue
                roles = []
                if approval.approver_employee_id == requester:
                    roles.append("requester")
                if approval.approver_employee_id in authors:
                    roles.append("author")
                if not roles:
                    continue
                role = "both" if len(roles) == 2 else roles[0]
                findings.append(
                    self.finding(
                        severity=Severity.HIGH,
                        entity=cid,
                        description=(
                            f"Change {cid} was approved ({approval.stage}) by "
                            f"{approval.approver_employee_id}, who is its {' and '.join(roles)}."
                        ),
                        evidence=data.hashes(
                            "changes", "change_approvals", "commits", "deployments"
                        ),
                        details={
                            "approver_employee_id": approval.approver_employee_id,
                            "approval_stage": approval.stage,
                            "role": role,
                        },
                        regulation_refs=[RTS_17_1_B],
                        context=_ctx(data),
                    )
                )
                break  # one finding per change
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# C-06  untested_change
# ---------------------------------------------------------------------------


class UntestedChange(Rule):
    """C-06: A normal medium/high risk change without a valid test sign-off.

    Trigger precedence when several apply: `missing`, `after_deploy`,
    `self_tested`.
    """

    rule_id = "C-06"
    version = "1.0.0"
    title = "Change deployed without test sign-off"

    def run(self, data: ChangeData) -> list[Finding]:  # type: ignore[override]
        findings: list[Finding] = []
        for cid in data.population:
            change = data.changes.loc[cid]
            if change["change_type"] != "normal" or change["risk"] not in ("medium", "high"):
                continue
            first = data.first_prod_deployment(cid)
            assert first is not None
            signoff = change["test_signoff_ts"]
            if signoff is None:
                trigger = "missing"
            elif signoff > first.deployed_ts:
                trigger = "after_deploy"
            elif change["tester_employee_id"] in data.commit_authors.get(cid, frozenset()):
                trigger = "self_tested"
            else:
                continue
            findings.append(
                self.finding(
                    severity=Severity.MEDIUM,
                    entity=cid,
                    description=(
                        f"Change {cid} ({change['risk']} risk) was deployed on "
                        f"{_iso(first.deployed_ts)} without a valid test sign-off ({trigger})."
                    ),
                    evidence=data.hashes("changes", "commits", "deployments"),
                    details={"trigger": trigger},
                    regulation_refs=[RTS_17_1_C_III],
                    context=_ctx(data),
                )
            )
        return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CHANGE_RULES = RuleRegistry()
for _cls in [
    DeployWithoutChange,
    SelfDeploy,
    EmergencyPostApproval,
    ApprovalAfterDeploy,
    ApproverNotIndependent,
    UntestedChange,
]:
    CHANGE_RULES.register(_cls)


def run_change_rules(
    data: ChangeData,
    params: dict[str, dict[str, Any]] | None = None,
) -> list[Finding]:
    """Run all six change rules and return the findings sorted by rule and entity.

    `params` maps a rule id to its parameters, e.g. ``{"C-03": {"post_days": 3}}``.
    Unknown rule ids raise `KeyError`.
    """
    params = params or {}
    unknown = sorted(set(params) - set(CHANGE_RULES.ids()))
    if unknown:
        raise KeyError(f"unknown change rule id(s): {', '.join(unknown)}")
    findings = CHANGE_RULES.run_all(data, params)
    return sorted(findings, key=lambda f: (f.rule_id, f.entity))
