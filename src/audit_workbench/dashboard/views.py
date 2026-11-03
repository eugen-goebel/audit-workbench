"""View models of the dashboard: plain data computed from one `Run`.

Nothing here imports Streamlit or plotly (a test guards that), so every number the dashboard
shows can be checked in a plain Python session. The app in `dashboard/app.py` only renders
what these functions return. All figures come from the same findings as `awb run`.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import networkx as nx
import pandas as pd

from audit_workbench.access.data import AccessData
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.journal.benford import THRESHOLDS, BenfordResult, benford_groups
from audit_workbench.journal.data import JournalData
from audit_workbench.regulation.mapping import load_mapping
from audit_workbench.reporting import (
    GroundTruthComparison,
    ModuleName,
    all_rule_ids,
    collect_evidence,
    compare_ground_truth,
    module_of,
    money,
    render_details,
    rule_summary,
    rule_title,
    rule_version,
    sorted_findings,
)
from audit_workbench.reporting import (
    findings_markdown as findings_markdown,
)
from audit_workbench.runner import Run

MODULES = ("A", "B", "C", "X")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")  # column order of the breakdown
HASH_PREFIX = 12
MAX_RAW_ROWS = 50

MIN_N: dict[str, int] = {"employee": 1000, "account": 5000}
"""Smallest group the Benford rules judge: B-01 (per employee) and B-02 (per account)."""


# --- overview -------------------------------------------------------------------


@dataclass(frozen=True)
class Kpis:
    findings: int
    critical: int
    rules_fired: int
    rules_run: int
    entities: int
    """Distinct entities (account, journal entry, change, employee, ...) with a finding."""


@dataclass(frozen=True)
class Overview:
    kpis: Kpis
    by_module_severity: pd.DataFrame
    """Rows A, B, C, X (or the one module run), columns LOW to CRITICAL, finding counts."""
    top_rules: pd.DataFrame
    """Every rule that ran, most findings first; a rule without findings has count 0."""
    data_basis: pd.DataFrame
    """One row per input file: name, rows, columns, SHA-256 and its 12-character prefix."""


def _severity_mix(counts: Counter[str]) -> str:
    parts = [f"{name} {counts[name]}" for name in reversed(SEVERITIES) if counts[name]]
    return ", ".join(parts) if parts else "-"


def overview(run: Run, module: ModuleName = ModuleName.all) -> Overview:
    """KPIs, findings per module and severity, the rule table and the data basis."""
    findings = run.findings
    rule_ids = all_rule_ids(module)
    fired = {f.rule_id for f in findings}
    kpis = Kpis(
        findings=len(findings),
        critical=sum(f.severity is Severity.CRITICAL for f in findings),
        rules_fired=len(fired),
        rules_run=len(rule_ids),
        entities=len({f.entity for f in findings}),
    )

    letters = MODULES if module is ModuleName.all else (module.value.upper(),)
    breakdown = pd.DataFrame(0, index=list(letters), columns=list(SEVERITIES), dtype=int)
    for f in findings:
        breakdown.loc[module_of(f.rule_id), f.severity.name] += 1

    per_rule: dict[str, Counter[str]] = {r: Counter() for r in rule_ids}
    for f in findings:
        per_rule.setdefault(f.rule_id, Counter())[f.severity.name] += 1
    ordered = sorted(per_rule, key=lambda r: (-sum(per_rule[r].values()), r))
    top = pd.DataFrame(
        {
            "rule": ordered,
            "title": [rule_title(r) for r in ordered],
            "module": [module_of(r) for r in ordered],
            "count": [sum(per_rule[r].values()) for r in ordered],
            "severity_mix": [_severity_mix(per_rule[r]) for r in ordered],
            **{name: [per_rule[r][name] for r in ordered] for name in reversed(SEVERITIES)},
        }
    )

    evidence = collect_evidence(run.access, run.journal, run.change)
    basis = pd.DataFrame(
        [
            {
                "file": ev.path.name,
                "rows": ev.rows,
                "columns": len(ev.frame.columns),
                "sha256": ev.sha256,
                "sha256_prefix": ev.sha256[:HASH_PREFIX],
            }
            for ev in sorted(evidence.values(), key=lambda e: e.path.name)
        ],
        columns=["file", "rows", "columns", "sha256", "sha256_prefix"],
    )
    return Overview(kpis, breakdown, top, basis)


def evidence_quality(
    run: Run, truth: dict[str, Any] | None, module: ModuleName = ModuleName.all
) -> GroundTruthComparison | None:
    """Findings against the planted issues, the comparison `awb check-ground-truth` runs.

    None when the data has no ground truth.
    """
    if truth is None:
        return None
    return compare_ground_truth(truth, run.findings, module)


# --- findings list and export ---------------------------------------------------


def module_rules(letter: str) -> list[tuple[str, str]]:
    """(rule id, title) of the rules of one module."""
    return [(r, rule_title(r)) for r in all_rule_ids(ModuleName(letter.lower()))]


def filter_findings(
    findings: Iterable[Finding],
    module: str | None = None,
    severities: Collection[str] | None = None,
    rule_ids: Collection[str] | None = None,
) -> list[Finding]:
    """Findings of one module, severities and rules (None = no restriction), most severe first."""
    return sorted_findings(
        [
            f
            for f in findings
            if (module is None or module_of(f.rule_id) == module)
            and (severities is None or f.severity.name in severities)
            and (rule_ids is None or f.rule_id in rule_ids)
        ]
    )


_FRAME_COLUMNS = ["finding_id", "module", "rule", "title", "severity", "entity", "description"]


def findings_frame(findings: Iterable[Finding]) -> pd.DataFrame:
    """One row per finding, in the order given."""
    return pd.DataFrame(
        [
            {
                "finding_id": f.finding_id,
                "module": module_of(f.rule_id),
                "rule": f.rule_id,
                "title": rule_title(f.rule_id),
                "severity": f.severity.name,
                "entity": f.entity,
                "description": f.description,
            }
            for f in findings
        ],
        columns=_FRAME_COLUMNS,
    )


def findings_csv(findings: Iterable[Finding]) -> str:
    """The findings as CSV text, generated in memory."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["finding_id", "module", "rule_id", "severity", "entity", "description"])
    for f in findings:
        writer.writerow(
            [f.finding_id, module_of(f.rule_id), f.rule_id, f.severity.name, f.entity]
            + [f.description]
        )
    return buffer.getvalue()


# --- finding detail -------------------------------------------------------------


@dataclass(frozen=True)
class RefView:
    """A regulation reference; the quote and URL exist when the mapping knows the provision."""

    label: str
    quote: str | None
    url: str | None


@dataclass(frozen=True)
class EvidenceRef:
    file: str
    sha256: str
    prefix: str


@dataclass(frozen=True)
class RawRows:
    """The rows of one evidence file that the finding names."""

    file: str
    key: str
    rows: pd.DataFrame
    total: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)


@dataclass(frozen=True)
class FindingDetail:
    finding: Finding
    rule_title: str
    rule_summary: str
    rule_version: str
    params: dict[str, Any]
    references: list[RefView]
    evidence: list[EvidenceRef]
    detail_lines: list[str]
    raw_rows: list[RawRows] = field(default_factory=list)


Getter = Callable[[Finding], list[str]]


def _entity(f: Finding) -> list[str]:
    return [f.entity]


def _part(index: int) -> Getter:
    return lambda f: [f.entity.split(":")[index]]


def _detail(name: str) -> Getter:
    def get(f: Finding) -> list[str]:
        value = f.details.get(name)
        if value is None:
            return []
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    return get


def _action_ids(*types: str) -> Getter:
    return lambda f: [
        str(a["id"])
        for a in f.details.get("actions", [])
        if isinstance(a, dict) and a["type"] in types
    ]


_ACCOUNT_ROLE = {"account_id": _part(0), "role_id": _part(1)}
_JOURNAL_ENTRY: list[tuple[str, dict[str, Getter]]] = [
    ("journal_entries", {"doc_no": _entity}),
    ("journal_lines", {"doc_no": _entity}),
]
_CHANGE: list[tuple[str, dict[str, Getter]]] = [
    (file, {"change_id": _entity})
    for file in ("changes", "change_approvals", "commits", "deployments")
]

# rule id -> [(evidence name, {column: where its value comes from})]; the columns of one file
# are combined with AND. B-01 and B-03 judge a whole population, so they have no rows to show.
_RAW_ROWS: dict[str, list[tuple[str, dict[str, Getter]]]] = {
    "A-01": [
        ("accounts", {"account_id": _entity}),
        ("hr_employees", {"employee_id": _detail("employee_id")}),
    ],
    "A-02": [("accounts", {"account_id": _entity})],
    "A-03": [("accounts", {"account_id": _entity})],
    "A-04": [("account_roles", _ACCOUNT_ROLE), ("recert_decisions", _ACCOUNT_ROLE)],
    "A-05": [("accounts", {"employee_id": _part(0)})],
    "A-06": [("recert_decisions", {"campaign_id": _part(0), "reviewer_employee_id": _part(1)})],
    "A-07": [("accounts", {"account_id": _entity})],
    "A-08": [("account_roles", _ACCOUNT_ROLE)],
    "B-02": [("journal_lines", {"account_no": _entity})],
    "B-04": _JOURNAL_ENTRY,
    "B-05": _JOURNAL_ENTRY,
    "B-06": [("journal_entries", {"doc_no": _detail("doc_no")})],
    "B-07": _JOURNAL_ENTRY,
    "B-08": _JOURNAL_ENTRY,
    "B-09": _JOURNAL_ENTRY,
    "B-10": _JOURNAL_ENTRY,
    "C-01": [("deployments", {"deployment_id": _entity})],
    "C-02": [("deployments", {"deployment_id": _entity})],
    "C-03": _CHANGE,
    "C-04": _CHANGE,
    "C-05": _CHANGE,
    "C-06": _CHANGE,
    "X-01": [
        ("accounts", {"employee_id": _part(0)}),
        ("journal_entries", {"doc_no": _detail("doc_nos")}),
        ("journal_lines", {"doc_no": _detail("doc_nos")}),
    ],
    "X-02": [("journal_entries", {"doc_no": _entity})],
    "X-03": [
        ("hr_employees", {"employee_id": _entity}),
        ("accounts", {"employee_id": _entity}),
        ("commits", {"commit_sha": _action_ids("commit")}),
        ("deployments", {"deployment_id": _action_ids("prod_deploy")}),
        ("journal_entries", {"doc_no": _action_ids("je_prepare", "je_approve")}),
    ],
}


def _raw_rows(run: Run, finding: Finding, max_rows: int) -> list[RawRows]:
    evidence = collect_evidence(run.access, run.journal, run.change)
    out: list[RawRows] = []
    for name, conditions in _RAW_ROWS.get(finding.rule_id, []):
        ev = evidence.get(name)
        if ev is None:
            continue
        mask = pd.Series(True, index=ev.frame.index)
        shown: list[str] = []
        for column, getter in conditions.items():
            keys = getter(finding)
            mask &= ev.frame[column].isin(keys)
            shown.append(
                f"{column} = {keys[0]}" if len(keys) == 1 else f"{column} in {len(keys)} values"
            )
        hits = ev.frame[mask]
        if hits.empty:
            continue
        out.append(RawRows(ev.path.name, ", ".join(shown), hits.head(max_rows), len(hits)))
    return out


def finding_detail(run: Run, finding: Finding, max_rows: int = MAX_RAW_ROWS) -> FindingDetail:
    """Everything the drill-down shows for one finding."""
    mapped = {str(ref): ref for ref in load_mapping().get(finding.rule_id, ())}
    references = [
        RefView(label, mapped[label].quote, mapped[label].url)
        if label in mapped
        else RefView(label, None, None)
        for label in finding.regulation_refs
    ]
    names = {
        ev.sha256: ev.path.name
        for ev in collect_evidence(run.access, run.journal, run.change).values()
    }
    evidence = [EvidenceRef(names.get(h, "?"), h, h[:HASH_PREFIX]) for h in finding.evidence]
    return FindingDetail(
        finding=finding,
        rule_title=rule_title(finding.rule_id),
        rule_summary=rule_summary(finding.rule_id),
        rule_version=rule_version(finding.rule_id),
        params=dict(finding.params),
        references=references,
        evidence=evidence,
        detail_lines=render_details(finding, run.access, run.journal, run.change),
        raw_rows=_raw_rows(run, finding, max_rows),
    )


# --- role graph -----------------------------------------------------------------


@dataclass(frozen=True)
class RoleGraphView:
    nodes: pd.DataFrame
    """node, kind (role or entitlement), name, system, privileged, layer, x, y."""
    edges: pd.DataFrame
    """source, target: the container points at what it contains."""


def role_graph_view(access: AccessData) -> RoleGraphView:
    """The role hierarchy with a layered layout: roles left, entitlements right."""
    graph = access.role_graph
    layers = {n: i for i, gen in enumerate(nx.topological_generations(graph)) for n in gen}
    rows: list[dict[str, Any]] = []
    for node, data in graph.nodes(data=True):
        if data["kind"] == "role":
            info = access.roles.loc[node]
            name, system, privileged = info["name"], info["system"], bool(info["privileged"])
        else:
            info = access.entitlements.loc[node]
            name, system, privileged = info["name"], info["system"], False
        rows.append(
            {
                "node": node,
                "kind": data["kind"],
                "name": name,
                "system": system,
                "privileged": privileged,
                "layer": layers[node],
            }
        )
    nodes = pd.DataFrame(rows).sort_values(["layer", "kind", "system", "node"], kind="stable")
    nodes["x"] = nodes["layer"].astype(float)
    position = nodes.groupby("layer").cumcount()
    size = nodes.groupby("layer")["node"].transform("size")
    nodes["y"] = position - (size - 1) / 2
    edges = pd.DataFrame(list(graph.edges), columns=["source", "target"])
    return RoleGraphView(nodes.reset_index(drop=True), edges)


@dataclass(frozen=True)
class SodHighlight:
    """The two access paths behind an A-05 conflict, as nodes and edges of the role graph."""

    finding_id: str
    paths: dict[str, list[str]]
    """Business function -> path from the account to the entitlement."""
    accounts: list[str]
    nodes: set[str]
    edges: set[tuple[str, str]]


def sod_highlight(access: AccessData, finding: Finding) -> SodHighlight:
    """Nodes, edges and accounts to highlight for an A-05 finding."""
    if finding.rule_id != "A-05":
        raise ValueError(f"only A-05 findings have access paths, got {finding.rule_id}")
    paths = {
        fn: [str(n) for n in path] for fn, path in finding.details.items() if isinstance(path, list)
    }
    nodes: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for path in paths.values():
        inside = path[1:]  # the first node is the account, which is not part of the role graph
        nodes |= set(inside)
        edges |= set(zip(inside, inside[1:], strict=False))
    return SodHighlight(
        finding_id=finding.finding_id,
        paths=paths,
        accounts=sorted({p[0] for p in paths.values()}),
        nodes=nodes & set(access.role_graph.nodes),
        edges=edges & set(access.role_graph.edges),
    )


@dataclass(frozen=True)
class RoleInfo:
    role_id: str
    name: str
    system: str
    privileged: bool
    accounts: pd.DataFrame
    """Accounts holding the role on the audit date: account_id, employee_id, via_role."""
    grants_roles: list[str]
    """Every role this role contains, transitively (`roles_within` without the role itself)."""
    entitlements: pd.DataFrame
    """Everything the role grants: entitlement_id, name, business_function."""


def role_info(access: AccessData, role_id: str) -> RoleInfo:
    """Accounts holding a role and everything it grants transitively; KeyError if unknown."""
    if role_id not in access.roles.index:
        raise KeyError(role_id)
    graph = access.role_graph
    info = access.roles.loc[role_id]
    containers = {role_id} | nx.ancestors(graph, role_id)
    active = access.active_assignments
    held = active[active["role_id"].isin(containers)][["account_id", "role_id"]]
    held = held.rename(columns={"role_id": "via_role"})
    held["employee_id"] = held["account_id"].map(access.accounts["employee_id"])
    held = held[["account_id", "employee_id", "via_role"]].sort_values(["account_id", "via_role"])
    granted = sorted(
        n for n in nx.descendants(graph, role_id) if graph.nodes[n]["kind"] == "entitlement"
    )
    return RoleInfo(
        role_id=role_id,
        name=str(info["name"]),
        system=str(info["system"]),
        privileged=bool(info["privileged"]),
        accounts=held.reset_index(drop=True),
        grants_roles=sorted(access.roles_within(role_id) - {role_id}),
        entitlements=access.entitlements.loc[
            granted, ["entitlement_id", "name", "business_function"]
        ].reset_index(drop=True),
    )


# --- Benford --------------------------------------------------------------------


@dataclass(frozen=True)
class BenfordGroup:
    key: str
    n: int
    mad: float
    chi_square: float
    conformity: str
    testable: bool
    nonconforming: bool
    """True only for a testable group whose MAD is above the nonconformity limit."""
    finding_id: str | None
    result: BenfordResult = field(repr=False, compare=False)

    @property
    def status(self) -> str:
        if not self.testable:
            return "not testable"
        return "nonconforming" if self.nonconforming else "conforming"


@dataclass(frozen=True)
class BenfordView:
    by: str
    digits: int
    min_n: int
    threshold: float
    """MAD above this is nonconformity (Nigrini)."""
    groups: list[BenfordGroup]


def benford_view(
    journal: JournalData | None,
    by: Literal["employee", "account"],
    digits: int | None = None,
    findings: Iterable[Finding] = (),
    min_n: int | None = None,
) -> BenfordView:
    """Digit tests per employee (B-01) or GL account (B-02).

    Groups below `min_n` are marked as not testable: too few amounts say nothing either way.
    A group that is also a B-01/B-02 finding carries its finding id (only for the digits the
    rule uses: one for employees, two for accounts).
    """
    if by not in MIN_N:
        raise ValueError("by must be 'employee' or 'account'")
    if digits not in (None, 1, 2):
        raise ValueError("digits must be 1 or 2")
    if journal is None:
        raise ValueError("the Benford view needs module B data")
    default_digits = 1 if by == "employee" else 2
    k = digits or default_digits
    limit = THRESHOLDS[k][2]
    threshold_n = MIN_N[by] if min_n is None else min_n
    rule = "B-01" if by == "employee" else "B-02"
    flagged = {f.entity: f.finding_id for f in findings if f.rule_id == rule}
    groups = []
    for key, result in sorted(benford_groups(journal, by, digits=k).items()):
        testable = result.n >= threshold_n
        groups.append(
            BenfordGroup(
                key=key,
                n=result.n,
                mad=result.mad,
                chi_square=result.chi_square,
                conformity=result.conformity,
                testable=testable,
                nonconforming=testable and result.mad > limit,
                finding_id=flagged.get(key) if k == default_digits else None,
                result=result,
            )
        )
    return BenfordView(by, k, threshold_n, limit, groups)


def benford_table(group: BenfordGroup) -> pd.DataFrame:
    """digit, expected, observed, count and z of one group."""
    return group.result.table()


# --- cross-module chains --------------------------------------------------------

_ACTION_MODULE = {
    "je_prepare": "B",
    "je_approve": "B",
    "prod_deploy": "C",
    "commit": "C",
    "change_approval": "C",
    "recert_decision": "A",
}


@dataclass(frozen=True)
class ChainStep:
    module: str
    """Module page the step belongs to (A, B or C)."""
    kind: str
    key: str
    """Entity of the step: account, journal entry, commit, ..."""
    label: str
    rule_id: str | None = None
    finding_id: str | None = None
    """The single-module finding behind this step, when there is one."""


@dataclass(frozen=True)
class Chain:
    finding: Finding
    title: str
    link_key: str
    """What joins the modules: the employee, the account or the journal entry."""
    steps: list[ChainStep]
    related: list[Finding]

    @property
    def modules(self) -> list[str]:
        return sorted({s.module for s in self.steps})


def _entry_label(journal: JournalData | None, doc_no: str) -> str:
    if journal is None:
        return doc_no
    entries = journal.unique_entries
    hit = entries[entries["doc_no"] == doc_no]
    if hit.empty:
        return doc_no
    row = hit.iloc[0]
    return (
        f"{doc_no} posted {row['posting_date']}, amount {money(row['amount_cents'])}, "
        f"prepared by {row['preparer_employee_id'] or '-'}, approved by {row['approver_employee_id'] or '-'}"
    )


def cross_chains(run: Run) -> list[Chain]:
    """The X findings as chains from one module to the next, with the linking key."""
    by_pair = {(f.rule_id, f.entity): f for f in run.findings}
    chains: list[Chain] = []
    for finding in (f for f in sorted_findings(run.findings) if module_of(f.rule_id) == "X"):
        details = finding.details
        related = [
            by_pair[(r, e)] for r, e in map(tuple, details.get("related", [])) if (r, e) in by_pair
        ]

        def step(
            module: str,
            kind: str,
            key: str,
            label: str,
            rule_id: str | None = None,
            entity: str | None = None,
        ) -> ChainStep:
            hit = by_pair.get((rule_id, entity)) if rule_id and entity else None
            return ChainStep(module, kind, key, label, rule_id, hit.finding_id if hit else None)

        steps: list[ChainStep] = []
        if finding.rule_id == "X-01":
            employee, conflict = finding.entity.split(":", 1)
            link_key = f"employee {employee}, conflict {conflict}"
            steps.append(
                step(
                    "A",
                    "SoD conflict",
                    finding.entity,
                    f"{employee} holds the conflicting functions of {conflict}",
                    "A-05",
                    finding.entity,
                )
            )
            for doc_no in details.get("doc_nos", []):
                related_rule = next((r.rule_id for r in related if r.entity == doc_no), None)
                steps.append(
                    step(
                        "B",
                        "journal entry",
                        doc_no,
                        _entry_label(run.journal, doc_no),
                        related_rule,
                        doc_no,
                    )
                )
        elif finding.rule_id == "X-02":
            doc_no = finding.entity
            checks = details.get("checks", [])
            account = checks[0]["account_id"] if checks else "?"
            link_key = f"journal entry {doc_no}, account {account}"
            for check in checks:
                steps.append(
                    step(
                        "A",
                        "access check",
                        check["account_id"],
                        f"{check['account_id']} did not hold {check['function']} on {check['ts']} "
                        f"({check['action']}, {check['reason']})",
                    )
                )
            related_rule = next((r.rule_id for r in related if r.entity == doc_no), None)
            steps.append(
                step(
                    "B",
                    "journal entry",
                    doc_no,
                    _entry_label(run.journal, doc_no),
                    related_rule,
                    doc_no,
                )
            )
        else:  # X-03
            link_key = f"employee {finding.entity}"
            a01 = next((r for r in related if r.rule_id == "A-01"), None)
            steps.append(
                step(
                    "A",
                    "exit",
                    finding.entity,
                    f"{finding.entity} left on {details.get('exit_date')}"
                    + (f"; account {a01.entity} still active" if a01 else ""),
                    "A-01" if a01 else None,
                    a01.entity if a01 else None,
                )
            )
            exit_date = pd.Timestamp(details["exit_date"]) if details.get("exit_date") else None
            for action in details.get("actions", []):
                ts = pd.Timestamp(action["ts"])
                after = (
                    f", {(ts.normalize() - exit_date).days} days after exit"
                    if exit_date is not None
                    else ""
                )
                steps.append(
                    step(
                        _ACTION_MODULE.get(action["type"], "C"),
                        "action",
                        str(action["id"]),
                        f"{action['type']} {action['id']} at {action['ts']}{after}",
                    )
                )
        chains.append(Chain(finding, rule_title(finding.rule_id), link_key, steps, related))
    return chains
