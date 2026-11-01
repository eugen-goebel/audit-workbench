"""Load the access-review input files and build the views the rules share.

Rules work on an `AccessData` object instead of raw frames. It parses dates
once, knows which assignments are active on the audit date and holds the
role hierarchy as a graph. Each rule still reports the hashes of the evidence
files it used.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from pathlib import Path

import networkx as nx
import pandas as pd

from audit_workbench.core.evidence import Evidence, EvidenceError, load_evidence

SCHEMA: dict[str, list[str]] = {
    "hr_employees": ["employee_id", "name", "org_unit", "job_function", "entry_date", "exit_date"],
    "accounts": [
        "account_id",
        "system",
        "employee_id",
        "account_type",
        "interactive",
        "status",
        "created_at",
        "last_login",
    ],
    "roles": ["role_id", "system", "name", "privileged"],
    "role_hierarchy": ["parent_role_id", "child_role_id"],
    "entitlements": ["entitlement_id", "system", "name", "business_function"],
    "role_entitlements": ["role_id", "entitlement_id"],
    "account_roles": ["account_id", "role_id", "valid_from", "valid_to"],
    "recert_campaigns": ["campaign_id", "system", "start_date", "end_date"],
    "recert_decisions": [
        "campaign_id",
        "account_id",
        "role_id",
        "reviewer_employee_id",
        "decision",
        "item_shown_at",
        "decided_at",
    ],
}


def load_access_evidence(directory: Path) -> dict[str, Evidence]:
    """Load every file of the access schema from one directory."""
    directory = Path(directory)
    return {
        name: load_evidence(directory / f"{name}.csv", name=name, required_columns=cols)
        for name, cols in SCHEMA.items()
    }


def _to_date(series: pd.Series) -> pd.Series:
    """Parse ISO dates to `datetime.date`, empty strings to None (not NaT,
    which cannot be compared with a date)."""

    def parse(value: str) -> date | None:
        return date.fromisoformat(value) if value else None

    try:
        return series.map(parse).astype(object)
    except ValueError as exc:
        raise EvidenceError(f"invalid date: {exc}") from exc


def _to_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.replace("", None), format="%Y-%m-%dT%H:%M:%S")


def _to_bool(series: pd.Series) -> pd.Series:
    lowered = series.str.lower()
    bad = ~lowered.isin(["true", "false"])
    if bad.any():
        raise EvidenceError(f"expected true/false, got {sorted(set(series[bad]))!r}")
    return lowered == "true"


@dataclass(frozen=True)
class AccessData:
    evidence: Mapping[str, Evidence]
    as_of: date

    def hashes(self, *names: str) -> list[str]:
        return [self.evidence[n].sha256 for n in names]

    # --- parsed tables -------------------------------------------------

    @cached_property
    def employees(self) -> pd.DataFrame:
        df = self.evidence["hr_employees"].frame.copy()
        df["entry_date"] = _to_date(df["entry_date"])
        df["exit_date"] = _to_date(df["exit_date"])
        df["has_left"] = df["exit_date"].map(lambda d: d is not None and d < self.as_of)
        return df.set_index("employee_id", drop=False)

    @cached_property
    def accounts(self) -> pd.DataFrame:
        df = self.evidence["accounts"].frame.copy()
        df["interactive"] = _to_bool(df["interactive"])
        df["created_at"] = _to_ts(df["created_at"])
        df["last_login"] = _to_ts(df["last_login"])
        return df.set_index("account_id", drop=False)

    @cached_property
    def roles(self) -> pd.DataFrame:
        df = self.evidence["roles"].frame.copy()
        df["privileged"] = _to_bool(df["privileged"])
        return df.set_index("role_id", drop=False)

    @cached_property
    def entitlements(self) -> pd.DataFrame:
        return self.evidence["entitlements"].frame.set_index("entitlement_id", drop=False)

    @cached_property
    def active_assignments(self) -> pd.DataFrame:
        """Role assignments valid on the audit date."""
        df = self.evidence["account_roles"].frame.copy()
        df["valid_from"] = _to_date(df["valid_from"])
        df["valid_to"] = _to_date(df["valid_to"])
        active = (df["valid_from"] <= self.as_of) & df["valid_to"].map(
            lambda d: d is None or d >= self.as_of
        )
        return df[active].reset_index(drop=True)

    @cached_property
    def campaigns(self) -> pd.DataFrame:
        df = self.evidence["recert_campaigns"].frame.copy()
        df["start_date"] = _to_date(df["start_date"])
        df["end_date"] = _to_date(df["end_date"])
        return df

    @cached_property
    def decisions(self) -> pd.DataFrame:
        df = self.evidence["recert_decisions"].frame.copy()
        df["item_shown_at"] = _to_ts(df["item_shown_at"])
        df["decided_at"] = _to_ts(df["decided_at"])
        return df

    # --- role graph ----------------------------------------------------

    @cached_property
    def role_graph(self) -> nx.DiGraph:
        """Directed graph: role -> child role -> ... -> entitlement.

        Edges point from the container to what it contains, so everything
        reachable from a role is what holding that role grants.
        """
        g = nx.DiGraph()
        for rid in self.roles.index:
            g.add_node(rid, kind="role")
        for eid in self.entitlements.index:
            g.add_node(eid, kind="entitlement")
        h = self.evidence["role_hierarchy"].frame
        g.add_edges_from(zip(h["parent_role_id"], h["child_role_id"], strict=True))
        re_ = self.evidence["role_entitlements"].frame
        g.add_edges_from(zip(re_["role_id"], re_["entitlement_id"], strict=True))
        cycles = list(
            nx.simple_cycles(g.subgraph([n for n in g if g.nodes[n].get("kind") == "role"]))
        )
        if cycles:
            raise EvidenceError(f"role_hierarchy contains cycles: {cycles[:3]!r}")
        return g

    def roles_within(self, role_id: str) -> set[str]:
        """The role itself and every role it contains, transitively."""
        g = self.role_graph
        return {role_id} | {n for n in nx.descendants(g, role_id) if g.nodes[n]["kind"] == "role"}

    def is_privileged(self, role_id: str) -> bool:
        return any(bool(self.roles.at[r, "privileged"]) for r in self.roles_within(role_id))
