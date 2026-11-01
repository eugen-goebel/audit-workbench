"""Load the change management input files and build the views the rules share.

Rules work on a `ChangeData` object instead of raw frames. It parses
timestamps once, resolves the account of every deployment to the employee
behind it (a deployer is an *employee*, not an account) and derives the
values several rules need: the population of the audit period, the first
production deployment of each change, its effective approval and the commit
authors. Each rule still reports the hashes of the evidence files it used.

Timestamps are kept as `datetime.datetime` or None, never as pandas `NaT`,
so a missing value cannot slip through a comparison unnoticed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cached_property
from pathlib import Path

import pandas as pd

from audit_workbench.access.data import SCHEMA as _ACCESS_SCHEMA
from audit_workbench.core.evidence import Evidence, EvidenceError, load_evidence

AS_OF = date(2026, 9, 30)
"""Audit date of module C, the same as module A (spec section 1)."""

SCHEMA: dict[str, list[str]] = {
    "changes": [
        "change_id",
        "system",
        "change_type",
        "risk",
        "title",
        "requester_employee_id",
        "created_ts",
        "test_signoff_ts",
        "tester_employee_id",
        "status",
    ],
    "change_approvals": [
        "change_id",
        "approval_stage",
        "approver_employee_id",
        "decision",
        "decided_ts",
    ],
    "commits": ["commit_sha", "system", "author_employee_id", "committed_ts", "change_id"],
    "deployments": [
        "deployment_id",
        "system",
        "environment",
        "deployed_ts",
        "account_id",
        "method",
        "released_by_employee_id",
        "change_id",
        "result",
    ],
    # module A files: they map accounts to employees
    "hr_employees": _ACCESS_SCHEMA["hr_employees"],
    "accounts": _ACCESS_SCHEMA["accounts"],
}

SYSTEMS = frozenset({"CORE", "PAY", "GL", "TRADE", "AD"})
_VALUE_SETS: dict[tuple[str, str], frozenset[str]] = {
    ("changes", "system"): SYSTEMS,
    ("changes", "change_type"): frozenset({"standard", "normal", "emergency"}),
    ("changes", "risk"): frozenset({"low", "medium", "high"}),
    ("changes", "status"): frozenset({"closed", "cancelled", "in_progress"}),
    ("change_approvals", "approval_stage"): frozenset({"pre", "post"}),
    ("change_approvals", "decision"): frozenset({"approved", "rejected"}),
    ("commits", "system"): SYSTEMS,
    ("deployments", "system"): SYSTEMS,
    ("deployments", "environment"): frozenset({"test", "prod"}),
    ("deployments", "method"): frozenset({"pipeline", "manual"}),
    ("deployments", "result"): frozenset({"success", "failed", "rolled_back"}),
}
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def load_change_evidence(directory: Path) -> dict[str, Evidence]:
    """Load every file of the change schema (and the module A files) from one directory."""
    directory = Path(directory)
    return {
        name: load_evidence(directory / f"{name}.csv", name=name, required_columns=cols)
        for name, cols in SCHEMA.items()
    }


def _preview(values: pd.Series, limit: int = 5) -> str:
    shown = [repr(v) for v in list(values.unique())[:limit]]
    return ", ".join(shown) + (", ..." if values.nunique() > limit else "")


def _timestamps(series: pd.Series, *, where: str, allow_empty: bool = False) -> pd.Series:
    """Parse ISO timestamps to `datetime` objects, empty strings to None."""

    def parse(value: str) -> datetime | None:
        if value == "":
            if allow_empty:
                return None
            raise EvidenceError(f"{where}: empty timestamp")
        try:
            return datetime.strptime(value, _TS_FORMAT)
        except ValueError as exc:
            raise EvidenceError(f"{where}: invalid timestamp {value!r}") from exc

    return pd.Series([parse(v) for v in series.astype(str)], index=series.index, dtype=object)


@dataclass(frozen=True)
class Deployment:
    """One production deployment with the employee behind it."""

    deployment_id: str
    system: str
    deployed_ts: datetime
    account_id: str
    method: str
    deploying_employee_id: str | None
    change_id: str
    result: str


@dataclass(frozen=True)
class Approval:
    change_id: str
    stage: str
    approver_employee_id: str
    decision: str
    decided_ts: datetime


@dataclass(frozen=True)
class ChangeData:
    evidence: Mapping[str, Evidence]
    as_of: date = AS_OF

    def __post_init__(self) -> None:
        # Data errors are reported at load time, not by a rule.
        for (name, column), allowed in _VALUE_SETS.items():
            values = self.evidence[name].frame[column]
            bad = ~values.isin(allowed)
            if bad.any():
                raise EvidenceError(f"{name}.{column}: unknown value {_preview(values[bad])}")
        _ = self.changes
        _ = self.approvals
        _ = self.commits
        _ = self.deployments

    def hashes(self, *names: str) -> list[str]:
        return [self.evidence[n].sha256 for n in names]

    # --- period -----------------------------------------------------------

    @property
    def period_start(self) -> date:
        """First day of the audit period: the 12 months up to `as_of`."""
        return date(self.as_of.year - 1, self.as_of.month, self.as_of.day) + timedelta(days=1)

    def in_period(self, ts: datetime) -> bool:
        return self.period_start <= ts.date() <= self.as_of

    # --- people -----------------------------------------------------------

    @cached_property
    def employees(self) -> pd.DataFrame:
        return self.evidence["hr_employees"].frame.set_index("employee_id", drop=False)

    @cached_property
    def accounts(self) -> pd.DataFrame:
        return self.evidence["accounts"].frame.set_index("account_id", drop=False)

    @cached_property
    def primary_ad_account(self) -> dict[str, str]:
        """Employee -> first personal AD account (by `created_at`, then `account_id`)."""
        df = self.evidence["accounts"].frame
        df = df[(df["system"] == "AD") & (df["account_type"] == "personal")]
        df = df.sort_values(["employee_id", "created_at", "account_id"])
        return dict(df.drop_duplicates("employee_id")[["employee_id", "account_id"]].values)

    # --- changes and approvals --------------------------------------------

    @cached_property
    def changes(self) -> pd.DataFrame:
        df = self.evidence["changes"].frame.copy()
        dup = df["change_id"].duplicated()
        if dup.any():
            raise EvidenceError(f"changes.change_id: duplicate {_preview(df['change_id'][dup])}")
        df["created_ts"] = _timestamps(df["created_ts"], where="changes.created_ts")
        df["test_signoff_ts"] = _timestamps(
            df["test_signoff_ts"], where="changes.test_signoff_ts", allow_empty=True
        )
        return df.set_index("change_id", drop=False)

    @cached_property
    def approvals(self) -> dict[str, list[Approval]]:
        """change_id -> approvals in time order (then file order)."""
        df = self.evidence["change_approvals"].frame
        ts = _timestamps(df["decided_ts"], where="change_approvals.decided_ts")
        out: dict[str, list[Approval]] = {}
        for row, decided in zip(df.itertuples(index=False), ts, strict=True):
            out.setdefault(row.change_id, []).append(
                Approval(
                    change_id=row.change_id,
                    stage=row.approval_stage,
                    approver_employee_id=row.approver_employee_id,
                    decision=row.decision,
                    decided_ts=decided,
                )
            )
        for items in out.values():
            items.sort(key=lambda a: a.decided_ts)
        return out

    def effective_approval(self, change_id: str) -> Approval | None:
        """The latest approved `pre` decision, unless a rejection came after it."""
        effective: Approval | None = None
        for a in self.approvals.get(change_id, []):
            if a.stage != "pre":
                continue
            effective = a if a.decision == "approved" else None
        return effective

    def approval_at(self, change_id: str, ts: datetime) -> Approval | None:
        """The approval in force at `ts`: the latest `pre` decision up to `ts`, if approved."""
        effective: Approval | None = None
        for a in self.approvals.get(change_id, []):
            if a.stage != "pre" or a.decided_ts > ts:
                continue
            effective = a if a.decision == "approved" else None
        return effective

    def first_post_approval(self, change_id: str) -> Approval | None:
        for a in self.approvals.get(change_id, []):
            if a.stage == "post" and a.decision == "approved":
                return a
        return None

    # --- commits ------------------------------------------------------------

    @cached_property
    def commits(self) -> pd.DataFrame:
        df = self.evidence["commits"].frame.copy()
        df["committed_ts"] = _timestamps(df["committed_ts"], where="commits.committed_ts")
        return df

    @cached_property
    def commit_authors(self) -> dict[str, frozenset[str]]:
        """change_id -> employees who authored a commit linked to it."""
        df = self.commits[self.commits["change_id"] != ""]
        return {
            cid: frozenset(group["author_employee_id"])
            for cid, group in df.groupby("change_id", sort=True)
        }

    def commits_of(self, change_id: str, author: str | None = None) -> list[str]:
        """Sorted commit SHAs of a change, optionally only those by one author."""
        df = self.commits[self.commits["change_id"] == change_id]
        if author is not None:
            df = df[df["author_employee_id"] == author]
        return sorted(df["commit_sha"])

    # --- deployments --------------------------------------------------------

    @cached_property
    def deployments(self) -> pd.DataFrame:
        """Every deployment row, with the deploying employee resolved."""
        df = self.evidence["deployments"].frame.copy()
        df["deployed_ts"] = _timestamps(df["deployed_ts"], where="deployments.deployed_ts")

        accounts = self.accounts
        unknown = ~df["account_id"].isin(accounts.index)
        if unknown.any():
            raise EvidenceError(
                f"deployments.account_id: unknown account {_preview(df['account_id'][unknown])}"
            )
        acct_type = df["account_id"].map(accounts["account_type"])
        manual_technical = (df["method"] == "manual") & (acct_type != "personal")
        if manual_technical.any():
            raise EvidenceError(
                "deployments: manual deployment with a non-personal account "
                f"{_preview(df['deployment_id'][manual_technical])}"
            )
        pipeline_no_release = (df["method"] == "pipeline") & (df["released_by_employee_id"] == "")
        if pipeline_no_release.any():
            raise EvidenceError(
                "deployments: pipeline deployment without released_by_employee_id "
                f"{_preview(df['deployment_id'][pipeline_no_release])}"
            )

        owner = df["account_id"].map(accounts["employee_id"])
        deploying = df["released_by_employee_id"].where(df["method"] == "pipeline", owner)
        df["deploying_employee_id"] = deploying.where(deploying != "", None).astype(object)
        return df

    @cached_property
    def prod_deployments(self) -> list[Deployment]:
        """Production deployments in the audit period, sorted by time, then id."""
        df = self.deployments[self.deployments["environment"] == "prod"]
        out = [
            Deployment(
                deployment_id=r.deployment_id,
                system=r.system,
                deployed_ts=r.deployed_ts,
                account_id=r.account_id,
                method=r.method,
                deploying_employee_id=r.deploying_employee_id,
                change_id=r.change_id,
                result=r.result,
            )
            for r in df.itertuples(index=False)
            if self.in_period(r.deployed_ts)
        ]
        return sorted(out, key=lambda d: (d.deployed_ts, d.deployment_id))

    @cached_property
    def deployments_by_change(self) -> dict[str, list[Deployment]]:
        """change_id -> its production deployments in the period, in time order."""
        out: dict[str, list[Deployment]] = {}
        for d in self.prod_deployments:
            if d.change_id:
                out.setdefault(d.change_id, []).append(d)
        return out

    @cached_property
    def population(self) -> list[str]:
        """Changes with at least one production deployment in the period, sorted."""
        known = set(self.changes.index)
        return sorted(c for c in self.deployments_by_change if c in known)

    def first_prod_deployment(self, change_id: str) -> Deployment | None:
        """Earliest production deployment of the change, whatever its result."""
        items = self.deployments_by_change.get(change_id)
        return items[0] if items else None


def load_change_data(directory: Path, as_of: date | None = None) -> ChangeData:
    """Load the change files of one directory; `as_of` defaults to 2026-09-30."""
    return ChangeData(evidence=load_change_evidence(directory), as_of=as_of or AS_OF)


__all__ = [
    "AS_OF",
    "SCHEMA",
    "Approval",
    "ChangeData",
    "Deployment",
    "load_change_data",
    "load_change_evidence",
]
