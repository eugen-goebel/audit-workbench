"""SoD ruleset loading and per-employee function reachability.

The SoD ruleset defines which pairs of business functions must not be held by
the same person. This module loads the YAML ruleset into pydantic models and
computes, for each employee, the business functions they can reach together with
one deterministic shortest access path per function.

An access path is a list of node ids from the account to the entitlement::

    [account_id, role_id, ..., entitlement_id]

Only active personal accounts (status=active, account_type=personal) and
assignments active on ``as_of`` are considered. Cross-account access within the
same employee counts (spec section 3), so an employee who can create payments
with one account and approve with another still has the conflict.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import yaml
from pydantic import BaseModel

from audit_workbench.access.data import AccessData
from audit_workbench.core.findings import Severity

_BUNDLED = Path(__file__).parent / "sod_ruleset.yaml"


class SodConflict(BaseModel):
    """One SoD conflict definition from the YAML ruleset."""

    model_config = {"frozen": True}

    id: str
    functions: tuple[str, str]
    severity: Severity
    description: str


def load_sod_ruleset(path: Path | None = None) -> list[SodConflict]:
    """Load SoD conflict definitions from a YAML file.

    Defaults to the bundled ``sod_ruleset.yaml`` next to this module.
    """
    p = Path(path) if path else _BUNDLED
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return [
        SodConflict(
            id=c["id"],
            functions=(c["functions"][0], c["functions"][1]),
            severity=Severity[c["severity"].upper()],
            description=c["description"],
        )
        for c in raw["conflicts"]
    ]


def employee_functions(data: AccessData) -> dict[str, dict[str, list[str]]]:
    """Compute the business functions held by each employee with one shortest path.

    Returns ``{employee_id: {business_function: path}}`` where *path* is a list
    of node ids ``[account_id, role_id, ..., entitlement_id]``.

    **Shortest** means fewest nodes from account to entitlement; ties are broken
    by sorting candidate paths lexicographically so the output is deterministic.

    Only active personal accounts and assignments active on ``as_of`` are
    considered. Cross-account access within the same employee counts.
    Unknown role ids (not present in the role graph) are silently skipped.
    """
    accts = data.accounts
    personal_active = accts[(accts["account_type"] == "personal") & (accts["status"] == "active")]

    assignments = data.active_assignments
    g = data.role_graph
    ents = data.entitlements

    # account_id -> employee_id, for active personal accounts only
    acct_to_emp: dict[str, str] = personal_active["employee_id"].to_dict()

    # account_id -> list[role_id], from active assignments for those accounts
    acct_roles: dict[str, list[str]] = {}
    for _, row in assignments.iterrows():
        aid = row["account_id"]
        if aid in acct_to_emp:
            acct_roles.setdefault(aid, []).append(row["role_id"])

    # employee_id -> {business_function -> list of candidate full paths}
    emp_candidates: dict[str, dict[str, list[list[str]]]] = {}

    for acct_id, emp_id in acct_to_emp.items():
        if not emp_id:
            continue
        for role_id in acct_roles.get(acct_id, []):
            if role_id not in g:
                continue
            # All entitlement nodes reachable from this role (transitive).
            # Graph construction guarantees every kind='entitlement' node is
            # in ents.index, and nx.descendants guarantees a path exists, so
            # no further defensive checks are needed here.
            desc = nx.descendants(g, role_id)
            ent_ids = [n for n in desc if g.nodes[n].get("kind") == "entitlement"]
            for ent_id in ent_ids:
                paths = list(nx.all_shortest_paths(g, role_id, ent_id))
                paths.sort()  # lex-sort for deterministic tie-breaking
                full_path = [acct_id] + paths[0]
                bf = ents.at[ent_id, "business_function"]

                cands = emp_candidates.setdefault(emp_id, {})
                cands.setdefault(bf, []).append(full_path)

    # For each (employee, function) keep the shortest path; ties broken lex
    result: dict[str, dict[str, list[str]]] = {}
    for emp_id, fn_paths in emp_candidates.items():
        result[emp_id] = {}
        for fn, paths in fn_paths.items():
            best = min(paths, key=lambda p: (len(p), p))
            result[emp_id][fn] = best

    return result
