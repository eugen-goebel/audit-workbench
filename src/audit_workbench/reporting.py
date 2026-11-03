"""Shared building blocks of the report commands: rule lookup, finding order, detail text.

`awb run`, `awb explain` and `awb workpaper` all describe findings the same way. The
helpers live here so the three commands (and the workpaper builder) cannot drift apart.
They do not depend on the command line framework.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from audit_workbench.access.data import AccessData
from audit_workbench.access.rules import ACCESS_RULES
from audit_workbench.change.data import ChangeData
from audit_workbench.change.rules import CHANGE_RULES
from audit_workbench.core.evidence import Evidence
from audit_workbench.core.findings import Finding
from audit_workbench.core.rules import Rule
from audit_workbench.crosslink import CROSSLINK_RULES
from audit_workbench.journal.data import JournalData
from audit_workbench.journal.rules import JOURNAL_RULES

_REGISTRIES = (ACCESS_RULES, JOURNAL_RULES, CHANGE_RULES, CROSSLINK_RULES)


class ModuleName(StrEnum):
    a = "a"
    b = "b"
    c = "c"
    x = "x"
    all = "all"


def module_of(rule_id: str) -> str:
    """Module letter of a rule id (`C-04` gives `C`); `?` for an id of no known module."""
    prefix, dash, _ = rule_id.partition("-")
    return prefix if dash and prefix in ("A", "B", "C", "X") else "?"


def sorted_findings(findings: list[Finding]) -> list[Finding]:
    """Module A, B, C, X in that order, then most severe first, then by rule and entity."""
    return sorted(findings, key=lambda f: (module_of(f.rule_id), -f.severity, f.rule_id, f.entity))


def rule_title(rule_id: str) -> str:
    if rule_id in ACCESS_RULES.ids():
        return ACCESS_RULES.get(rule_id).title
    if rule_id in JOURNAL_RULES.ids():
        return JOURNAL_RULES.get(rule_id).title
    if rule_id in CHANGE_RULES.ids():
        return CHANGE_RULES.get(rule_id).title
    if rule_id in CROSSLINK_RULES.ids():
        return CROSSLINK_RULES.get(rule_id).title
    return "(unknown rule)"


def all_rule_ids(module: ModuleName = ModuleName.all, *extra: str) -> list[str]:
    ids: list[str] = []
    if module in (ModuleName.a, ModuleName.all):
        ids += ACCESS_RULES.ids()
    if module in (ModuleName.b, ModuleName.all):
        ids += JOURNAL_RULES.ids()
    if module in (ModuleName.c, ModuleName.all):
        ids += CHANGE_RULES.ids()
    if module in (ModuleName.x, ModuleName.all):
        ids += CROSSLINK_RULES.ids()
    return sorted({*ids, *extra})


def collect_evidence(*datas: AccessData | JournalData | ChangeData | None) -> dict[str, Evidence]:
    """Every input file of the loaded modules, once per file name."""
    out: dict[str, Evidence] = {}
    for data in datas:
        if data is None:
            continue
        for name, evidence in data.evidence.items():
            out.setdefault(name, evidence)
    return out


def text_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]
    return "\n".join(lines)


def md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(md_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def findings_markdown(findings: list[Finding]) -> str:
    """The findings as the GitHub-markdown table `awb run --format markdown` prints."""
    if not findings:
        return "No findings."
    rows = [
        [f.finding_id, module_of(f.rule_id), f.rule_id, f.severity.name, f.entity, f.description]
        for f in findings
    ]
    return md_table(["finding_id", "module", "rule", "severity", "entity", "description"], rows)


def percent(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{100 * numerator / denominator:.1f}%"


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


@dataclass(frozen=True)
class GroundTruthComparison:
    """Findings against the issues planted in `ground_truth.json`, as sets of (rule, entity).

    `awb check-ground-truth` and the dashboard both use this, so they cannot disagree.
    """

    truth: dict[str, Any]
    expected: frozenset[tuple[str, str]]
    found: frozenset[tuple[str, str]]

    @property
    def matched(self) -> frozenset[tuple[str, str]]:
        return self.expected & self.found

    @property
    def missed(self) -> frozenset[tuple[str, str]]:
        """Planted but not found."""
        return self.expected - self.found

    @property
    def unexpected(self) -> frozenset[tuple[str, str]]:
        """Found but not planted."""
        return self.found - self.expected

    @property
    def is_exact(self) -> bool:
        return self.expected == self.found

    @property
    def recall(self) -> float | None:
        """Share of the planted issues that were found; None if nothing was planted."""
        return _ratio(len(self.matched), len(self.expected))

    @property
    def precision(self) -> float | None:
        """Share of the findings that were planted; None if there are no findings."""
        return _ratio(len(self.matched), len(self.found))


def compare_ground_truth(
    truth: dict[str, Any], findings: Iterable[Finding], module: ModuleName = ModuleName.all
) -> GroundTruthComparison:
    """Compare findings with the planted issues; `module` limits the planted issues to the
    modules that were run, so a single-module run is not blamed for the others."""
    found = frozenset((f.rule_id, f.entity) for f in findings)
    expected = frozenset(
        (i["rule_id"], i["entity"])
        for i in truth["issues"]
        if module is ModuleName.all or module_of(i["rule_id"]) == module.value.upper()
    )
    return GroundTruthComparison(truth=truth, expected=expected, found=found)


def ground_truth_report(comparison: GroundTruthComparison) -> str:
    """The GitHub-markdown report of `awb check-ground-truth`."""
    truth, expected, found = comparison.truth, comparison.expected, comparison.found
    rule_ids = all_rule_ids(ModuleName.all, *(r for r, _ in expected | found))
    rows: list[list[str]] = []
    totals: dict[str, list[int]] = {}
    for rule_id in rule_ids:
        module = module_of(rule_id)
        exp = {e for r, e in expected if r == rule_id}
        got = {e for r, e in found if r == rule_id}
        hits = len(exp & got)
        t = totals.setdefault(module, [0, 0, 0])
        t[0] += len(exp)
        t[1] += len(got)
        t[2] += hits
        rows.append(
            [module, rule_id, rule_title(rule_id), str(len(exp)), str(len(got)), str(hits)]
            + [percent(hits, len(exp)), percent(hits, len(got))]
        )
    for module, (exp_n, got_n, hit_n) in sorted(totals.items()):
        rows.append(
            [module, f"**Total {module}**", "", str(exp_n), str(got_n), str(hit_n)]
            + [percent(hit_n, exp_n), percent(hit_n, got_n)]
        )
    hits = len(expected & found)
    rows.append(
        ["", "**Total**", "", str(len(expected)), str(len(found)), str(hits)]
        + [percent(hits, len(expected)), percent(hits, len(found))]
    )
    verdict = "PASS" if comparison.is_exact else "FAIL"
    as_of_b = truth.get("journal_as_of", truth.get("as_of"))
    as_of_c = truth.get("change_as_of", truth.get("as_of"))
    out = [
        "## Ground-truth check",
        "",
        f"- Seed: {truth.get('seed')}",
        f"- As of module A: {truth.get('as_of')}",
        f"- As of module B: {as_of_b}",
        f"- As of module C: {as_of_c}",
        f"- Result: **{verdict}**",
        "",
        md_table(
            [
                "module",
                "rule",
                "title",
                "expected",
                "found",
                "true positives",
                "recall",
                "precision",
            ],
            rows,
        ),
    ]
    for heading, items in (
        ("Missed (expected, not found)", expected - found),
        ("Unexpected (found, not expected)", found - expected),
    ):
        if items:
            out += ["", f"### {heading}", ""]
            out += [f"- `{rule}` `{entity}`" for rule, entity in sorted(items)]
    return "\n".join(out)


def money(cents: int) -> str:
    return str(Decimal(int(cents)).scaleb(-2))


def benford_detail(details: dict[str, Any]) -> str:
    """Digit table of a B-01/B-02 finding: expected versus observed proportions."""
    digits = details.get("digits") or []
    rows = [
        [
            str(d["digit"]),
            f"{d['expected']:.4%}",
            f"{d['observed']:.4%}",
            str(d["count"]),
            f"{d['z']:+.2f}",
        ]
        for d in digits
    ]
    summary = (
        f"  n: {details.get('n')}, MAD: {details.get('mad'):.5f} "
        f"(nonconformity limit {details.get('nonconformity_threshold')}), "
        f"chi-square: {details.get('chi_square'):.2f}, "
        f"assessment: {details.get('conformity')}"
    )
    return text_table(["digit", "expected", "observed", "count", "z"], rows) + "\n" + summary


def journal_entry_detail(journal: JournalData, doc_no: str) -> list[str]:
    """Header and lines of one journal entry, with the people behind the accounts."""
    entries = journal.unique_entries
    hit = entries[entries["doc_no"] == doc_no]
    if hit.empty:
        return []
    row = hit.iloc[0]
    people = journal.employees

    def person(employee_id: Any) -> str:
        if not isinstance(employee_id, str) or employee_id not in people.index:
            return "-"
        return f"{employee_id} ({people.at[employee_id, 'name']})"

    lines = [
        f"  doc_no:       {row['doc_no']}",
        f"  source:       {row['source']}",
        f"  posting date: {row['posting_date']}",
        f"  entered:      {row['entry_ts']}",
        f"  preparer:     {person(row['preparer_employee_id'])}",
        f"  approver:     {person(row['approver_employee_id'])}",
        f"  approved:     {row['approved_ts'] or '-'}",
        f"  amount:       {money(row['amount_cents'])}",
        f"  description:  {row['description']}",
        "",
        "  Lines:",
    ]
    rows = [
        [account, money(debit), money(credit)]
        for account, debit, credit in journal.lines_by_doc.get(doc_no, [])
    ]
    lines.append("  " + text_table(["account", "debit", "credit"], rows).replace("\n", "\n  "))
    return lines


def _ts(ts: datetime) -> str:
    return ts.isoformat()


def change_detail(change: ChangeData, finding: Finding) -> list[str]:
    """The change behind a C finding: its record, approvals, commits and deployments.

    C-01 and C-02 are reported per deployment (the entity is a deployment id); all other
    rules are reported per change. A deployment without a change (or with one that is not in
    the change file) is shown on its own, since there is no change record to print.
    """
    people = change.employees

    def person(employee_id: Any) -> str:
        if not isinstance(employee_id, str) or employee_id not in people.index:
            return "-"
        return f"{employee_id} ({people.at[employee_id, 'name']})"

    flagged = None
    change_id = finding.entity
    if finding.rule_id in ("C-01", "C-02"):
        flagged = next(
            (d for d in change.prod_deployments if d.deployment_id == finding.entity), None
        )
        change_id = flagged.change_id if flagged is not None else ""
        lines = [f"  Flagged deployment: {finding.entity}", ""]
    else:
        lines = []

    deployments = change.deployments_by_change.get(change_id, [])
    if flagged is not None and not deployments:
        deployments = [flagged]  # no change to list it under
    if change_id in change.changes.index:
        row = change.changes.loc[change_id]
        signoff = (
            "none"
            if row["test_signoff_ts"] is None
            else f"{_ts(row['test_signoff_ts'])} by {person(row['tester_employee_id'])}"
        )
        lines += [
            "  Change:",
            f"    change_id:     {change_id}",
            f"    system:        {row['system']}",
            f"    type:          {row['change_type']}",
            f"    risk:          {row['risk']}",
            f"    title:         {row['title']}",
            f"    requester:     {person(row['requester_employee_id'])}",
            f"    created:       {_ts(row['created_ts'])}",
            f"    status:        {row['status']}",
            f"    test sign-off: {signoff}",
            "",
            "  Approvals (time order):",
        ]
        approvals = change.approvals.get(change_id, [])
        if approvals:
            lines.append(
                text_table(
                    ["stage", "approver", "decision", "decided"],
                    [
                        [a.stage, person(a.approver_employee_id), a.decision, _ts(a.decided_ts)]
                        for a in approvals
                    ],
                ).replace("\n", "\n    ")
            )
            lines[-1] = "    " + lines[-1]
        else:
            lines.append("    none")
        commits = change.commits[change.commits["change_id"] == change_id].sort_values(
            ["committed_ts", "commit_sha"]
        )
        lines += ["", "  Linked commits:"]
        if commits.empty:
            lines.append("    none")
        else:
            table = text_table(
                ["commit", "author", "committed"],
                [
                    [c.commit_sha, person(c.author_employee_id), _ts(c.committed_ts)]
                    for c in commits.itertuples(index=False)
                ],
            )
            lines.append("    " + table.replace("\n", "\n    "))
    elif flagged is not None:
        reason = (
            "the deployment carries no change id"
            if not change_id
            else f"change {change_id} is not in changes.csv"
        )
        lines += [f"  Change: none ({reason})"]
    if deployments:
        lines += ["", "  Production deployments:"]
        table = text_table(
            ["deployment", "deployed", "method", "account", "deploying employee", "result"],
            [
                [
                    d.deployment_id,
                    _ts(d.deployed_ts),
                    d.method,
                    d.account_id,
                    person(d.deploying_employee_id),
                    d.result,
                ]
                for d in deployments
            ],
        )
        lines.append("    " + table.replace("\n", "\n    "))
    return lines


def crosslink_detail(journal: JournalData | None, finding: Finding) -> list[str]:
    """A cross-module finding: the journal entries it rests on, then the rule details.

    The entity is `<employee>:<conflict>`, so the entries come from `details["doc_nos"]`
    rather than from the entity name.
    """
    out: list[str] = []
    doc_nos = [str(doc) for doc in finding.details.get("doc_nos") or []]
    if journal is not None:
        for doc_no in doc_nos:
            out += journal_entry_detail(journal, doc_no)
            out.append("")
    out += [
        "  Rule details:",
        *[f"    {key}: {json.dumps(value, default=str)}" for key, value in finding.details.items()],
    ]
    return out


def render_details(
    finding: Finding,
    access: AccessData | None,
    journal: JournalData | None,
    change: ChangeData | None = None,
) -> list[str]:
    """Rule-specific detail: access paths for A-05, digit tables and entries for B, the change
    with approvals, commits and deployments for C."""
    if finding.rule_id == "A-05":
        return [f"  {fn}: {' -> '.join(path)}" for fn, path in finding.details.items()]
    if module_of(finding.rule_id) == "X":
        return crosslink_detail(journal, finding)
    if finding.rule_id in ("B-01", "B-02"):
        return benford_detail(finding.details).splitlines()
    if journal is not None:
        entry = journal_entry_detail(journal, finding.entity)
        if entry:
            return entry
    rule_details = [
        f"{key}: {json.dumps(value, default=str)}" for key, value in finding.details.items()
    ]
    if change is not None and module_of(finding.rule_id) == "C":
        return [
            *change_detail(change, finding),
            "",
            "  Rule details:",
            *[f"    {line}" for line in rule_details],
        ]
    return [f"  {line}" for line in rule_details]


def rule_class(rule_id: str) -> type[Rule] | None:
    """The rule class registered under `rule_id`, or None for an unknown id."""
    for registry in _REGISTRIES:
        if rule_id in registry.ids():
            return registry.get(rule_id)
    return None


def rule_summary(rule_id: str) -> str:
    """What a rule tests: the first paragraph of its docstring, without the `A-01:` prefix."""
    cls = rule_class(rule_id)
    doc = (cls.__doc__ or "") if cls is not None else ""
    first = " ".join(doc.strip().split("\n\n")[0].split())
    prefix = f"{rule_id}:"
    return first[len(prefix) :].strip() if first.startswith(prefix) else first


def rule_version(rule_id: str) -> str:
    """The version of the rule registered under `rule_id`, or `?` for an unknown id."""
    cls = rule_class(rule_id)
    return cls.version if cls is not None else "?"
