"""The `awb` command line: run the audit tests and check them against ground truth.

Typical audit workflow:

1. `awb generate`            build a synthetic bank (or point the tools at your own extract),
2. `awb run`                 execute the access and journal-entry rules and list the exceptions,
3. `awb explain`             drill into one exception: rule, parameters, evidence, access path,
                             or the journal entry with its lines,
4. `awb benford`             look at the digit distributions behind the Benford findings,
5. `awb check-ground-truth`  prove the rules find exactly the planted issues (used in CI).

Every command is deterministic: same files and same audit date give the same findings
and the same finding ids. Module A is the access review, module B the journal-entry
testing.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.access.rules import ACCESS_RULES, run_access_rules
from audit_workbench.core.evidence import Evidence, EvidenceError
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.journal.anomaly import rank_entries
from audit_workbench.journal.benford import benford_groups
from audit_workbench.journal.data import JournalData, load_journal_data
from audit_workbench.journal.rules import JOURNAL_RULES, run_journal_rules
from audit_workbench.synth import generate_bank

GROUND_TRUTH_FILE = "ground_truth.json"
EXIT_FAILED = 1  # the check ran and found a problem (unknown finding, ground-truth mismatch)
EXIT_USAGE = 2  # the command could not run (missing input, missing audit date)
_DESCRIPTION_WIDTH = 70

app = typer.Typer(
    name="awb",
    help="Audit workbench: access review and journal-entry tests on bank IT data.",
    no_args_is_help=True,
    add_completion=False,
)

DataDir = Annotated[
    Path,
    typer.Argument(
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        help="Folder with the input CSVs.",
    ),
]
AsOfOption = Annotated[
    datetime | None,
    typer.Option(
        "--as-of",
        formats=["%Y-%m-%d"],
        help="Audit reference date (YYYY-MM-DD). Default: the date in ground_truth.json.",
    ),
]


class OutputFormat(StrEnum):
    table = "table"
    json = "json"
    markdown = "markdown"


class SeverityName(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ModuleName(StrEnum):
    a = "a"
    b = "b"
    all = "all"


# --- helpers -----------------------------------------------------------------


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _read_ground_truth(directory: Path) -> dict[str, Any] | None:
    path = directory / GROUND_TRUTH_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_a(directory: Path, as_of: datetime | None, truth: dict[str, Any] | None) -> date:
    """`--as-of` wins; otherwise take the access-review date from ground_truth.json."""
    if as_of is not None:
        return as_of.date()
    if truth is None or "as_of" not in truth:
        raise _fail(
            f"--as-of is required because {directory / GROUND_TRUTH_FILE} does not exist",
            EXIT_USAGE,
        )
    return date.fromisoformat(truth["as_of"])


def _resolve_b(directory: Path, as_of: datetime | None, truth: dict[str, Any] | None) -> date:
    """`--as-of` wins; otherwise the journal date (`journal_as_of`) or the bank's `as_of`."""
    if as_of is not None:
        return as_of.date()
    for key in ("journal_as_of", "as_of"):
        if truth is not None and key in truth:
            return date.fromisoformat(truth[key])
    raise _fail(
        f"--as-of is required for module B because {directory / GROUND_TRUTH_FILE} "
        "has no journal_as_of",
        EXIT_USAGE,
    )


def _load(
    directory: Path, as_of: datetime | None, module: ModuleName
) -> tuple[AccessData | None, JournalData | None, list[Finding]]:
    """Load the evidence the selected modules need and run their rules."""
    truth = _read_ground_truth(directory)
    access: AccessData | None = None
    journal: JournalData | None = None
    findings: list[Finding] = []
    try:
        if module in (ModuleName.a, ModuleName.all):
            access = AccessData(
                evidence=load_access_evidence(directory), as_of=_resolve_a(directory, as_of, truth)
            )
            findings += run_access_rules(access)
        if module in (ModuleName.b, ModuleName.all):
            journal = load_journal_data(directory, as_of=_resolve_b(directory, as_of, truth))
            findings += run_journal_rules(journal)
    except EvidenceError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    return access, journal, findings


def _module_of(rule_id: str) -> str:
    return "A" if rule_id.startswith("A-") else "B"


def _sorted(findings: list[Finding]) -> list[Finding]:
    """Module A first, then most severe first, then by rule and entity."""
    return sorted(findings, key=lambda f: (_module_of(f.rule_id), -f.severity, f.rule_id, f.entity))


def _rule_title(rule_id: str) -> str:
    if rule_id in ACCESS_RULES.ids():
        return ACCESS_RULES.get(rule_id).title
    if rule_id in JOURNAL_RULES.ids():
        return JOURNAL_RULES.get(rule_id).title
    return "(unknown rule)"


def _all_rule_ids(module: ModuleName = ModuleName.all, *extra: str) -> list[str]:
    ids: list[str] = []
    if module in (ModuleName.a, ModuleName.all):
        ids += ACCESS_RULES.ids()
    if module in (ModuleName.b, ModuleName.all):
        ids += JOURNAL_RULES.ids()
    return sorted({*ids, *extra})


def _evidence(*datas: AccessData | JournalData | None) -> dict[str, Evidence]:
    """Every input file of the loaded modules, once per file name."""
    out: dict[str, Evidence] = {}
    for data in datas:
        if data is None:
            continue
        for name, evidence in data.evidence.items():
            out.setdefault(name, evidence)
    return out


def _short(text: str, width: int = _DESCRIPTION_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _text_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _percent(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{100 * numerator / denominator:.1f}%"


def _money(cents: int) -> str:
    return str(Decimal(int(cents)).scaleb(-2))


# --- run: renderers ----------------------------------------------------------


def _render_table(
    findings: list[Finding],
    evidence: dict[str, Evidence],
    as_of_a: date | None,
    as_of_b: date | None,
    module: ModuleName,
) -> str:
    out: list[str] = []
    if as_of_a is not None:
        out.append(f"Audit date (as_of): {as_of_a.isoformat()}")
    if as_of_b is not None:
        out.append(f"Journal as_of: {as_of_b.isoformat()}")
    out.append("")
    if findings:
        rows = [
            [
                f.finding_id,
                _module_of(f.rule_id),
                f.rule_id,
                f.severity.name,
                f.entity,
                _short(f.description),
            ]
            for f in findings
        ]
        out.append(
            _text_table(["finding_id", "module", "rule", "severity", "entity", "description"], rows)
        )
    else:
        out.append("No findings.")
    counts = Counter(f.rule_id for f in findings)
    out += ["", "Findings per rule:"]
    out.append(
        _text_table(
            ["module", "rule", "title", "count"],
            [
                [_module_of(r), r, _rule_title(r), str(counts[r])]
                for r in _all_rule_ids(module, *counts)
            ]
            + [["", "Total", "", str(len(findings))]],
        )
    )
    out += ["", "Input files (SHA-256, first 12 characters):"]
    out.append(
        _text_table(
            ["file", "sha256", "rows"],
            [
                [ev.path.name, ev.sha256[:12], str(ev.rows)]
                for _, ev in sorted(evidence.items(), key=lambda item: item[1].path.name)
            ],
        )
    )
    return "\n".join(out)


def _render_rank(journal: JournalData, top: int) -> str:
    ranked = rank_entries(journal, top=top)
    if ranked.empty:
        return "No manual entries to rank."
    rows = [
        [str(row.rank), f"{row.score:.4f}", row.doc_no, row.preparer_employee_id]
        for row in ranked.itertuples(index=False)
    ]
    return _text_table(["rank", "score", "doc_no", "preparer"], rows)


def _render_json(findings: list[Finding]) -> str:
    return json.dumps([f.model_dump(mode="json") for f in findings], indent=2)


def _render_markdown(findings: list[Finding]) -> str:
    if not findings:
        return "No findings."
    rows = [
        [f.finding_id, _module_of(f.rule_id), f.rule_id, f.severity.name, f.entity, f.description]
        for f in findings
    ]
    return _md_table(["finding_id", "module", "rule", "severity", "entity", "description"], rows)


# --- commands ----------------------------------------------------------------


@app.command()
def generate(
    out_dir: Annotated[Path, typer.Argument(help="Folder to write the CSVs into (created).")],
    seed: Annotated[int, typer.Option(help="Random seed; same seed gives identical bytes.")] = 7,
    clean: Annotated[bool, typer.Option(help="Write a bank without injected issues.")] = False,
) -> None:
    """Create a synthetic bank with planted issues and a ground_truth.json.

    Use this to get reproducible test data. With --clean no issues are planted, which shows
    that the rules stay silent on healthy data. The bank contains the access data of module A
    and the journal ledger of module B.
    """
    generate_bank(out_dir, seed=seed, inject=not clean)
    evidence = load_access_evidence(out_dir)
    issues = len(_read_ground_truth(out_dir)["issues"])  # type: ignore[index]
    typer.echo(
        f"{out_dir}: {evidence['hr_employees'].rows} employees, "
        f"{evidence['accounts'].rows} accounts, {issues} injected issues"
    )


@app.command()
def run(
    data_dir: DataDir,
    as_of: AsOfOption = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="Output format.")
    ] = OutputFormat.table,
    output: Annotated[
        Path | None, typer.Option("--output", help="Write to this file instead of stdout.")
    ] = None,
    min_severity: Annotated[
        SeverityName, typer.Option(help="Hide findings below this severity.")
    ] = SeverityName.low,
    module: Annotated[
        ModuleName, typer.Option("--module", help="Run module A (access), B (journal) or all.")
    ] = ModuleName.all,
    rank: Annotated[
        int | None,
        typer.Option("--rank", help="Append the top-N anomaly ranking as a selection aid."),
    ] = None,
) -> None:
    """Run the audit rules and list the exceptions, most severe first.

    By default module A (access review) and module B (journal-entry testing) run together.
    The table ends with finding counts per rule and the SHA-256 prefix of every input file,
    so the run can be tied to the exact data it tested. Use --format json for the full
    finding records (ids, parameters, evidence hashes) and --format markdown for a report.
    --rank appends the anomaly ranking of manual journal entries; it is a sampling aid for
    choosing entries to inspect and deliberately does not produce findings.
    """
    access, journal, findings = _load(data_dir, as_of, module)
    threshold = Severity[min_severity.name.upper()]
    findings = _sorted([f for f in findings if f.severity >= threshold])

    if rank is not None:
        if output_format is OutputFormat.json:
            raise _fail("--rank cannot be combined with --format json", EXIT_USAGE)
        if journal is None:
            raise _fail("--rank needs module B data (--module b or --module all)", EXIT_USAGE)

    as_of_a = access.as_of if access is not None else None
    as_of_b = journal.as_of if journal is not None else None

    if output_format is OutputFormat.json:
        text = _render_json(findings)
    elif output_format is OutputFormat.markdown:
        text = _render_markdown(findings)
        if rank is not None and journal is not None:
            text += "\n\n### Anomaly ranking (selection aid, not findings)\n\n" + _render_rank(
                journal, rank
            )
    else:
        text = _render_table(findings, _evidence(access, journal), as_of_a, as_of_b, module)
        if rank is not None and journal is not None:
            text += (
                "\n\nAnomaly ranking (selection aid, not findings), top "
                f"{rank}:\n\n" + _render_rank(journal, rank)
            )

    if output is None:
        typer.echo(text)
    else:
        output.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"wrote {len(findings)} findings to {output}", err=True)


def _find_by_id_or_prefix(findings: list[Finding], wanted: str) -> Finding:
    exact = [f for f in findings if f.finding_id == wanted]
    matches = exact or [f for f in findings if f.finding_id.startswith(wanted)]
    if not matches:
        raise _fail(f"no finding with id {wanted!r}", EXIT_FAILED)
    if len(matches) > 1:
        ids = ", ".join(sorted(f.finding_id for f in matches))
        raise _fail(f"{wanted!r} is ambiguous, it matches: {ids}", EXIT_FAILED)
    return matches[0]


def _benford_detail(details: dict[str, Any]) -> str:
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
    return _text_table(["digit", "expected", "observed", "count", "z"], rows) + "\n" + summary


def _journal_entry_detail(journal: JournalData, doc_no: str) -> list[str]:
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
        f"  amount:       {_money(row['amount_cents'])}",
        f"  description:  {row['description']}",
        "",
        "  Lines:",
    ]
    rows = [
        [account, _money(debit), _money(credit)]
        for account, debit, credit in journal.lines_by_doc.get(doc_no, [])
    ]
    lines.append("  " + _text_table(["account", "debit", "credit"], rows).replace("\n", "\n  "))
    return lines


def _render_details(
    finding: Finding, access: AccessData | None, journal: JournalData | None
) -> list[str]:
    """Rule-specific detail: access paths for A-05, digit tables and entries for B."""
    if finding.rule_id == "A-05":
        return [f"  {fn}: {' -> '.join(path)}" for fn, path in finding.details.items()]
    if finding.rule_id in ("B-01", "B-02"):
        return _benford_detail(finding.details).splitlines()
    if journal is not None:
        entry = _journal_entry_detail(journal, finding.entity)
        if entry:
            return entry
    return [f"  {key}: {json.dumps(value, default=str)}" for key, value in finding.details.items()]


@app.command()
def explain(
    data_dir: DataDir,
    finding_id: Annotated[str, typer.Argument(help="Finding id, or a unique prefix of it.")],
    as_of: AsOfOption = None,
) -> None:
    """Show everything known about one finding.

    Prints the rule and version, severity, entity, the parameters it ran with, the evidence
    files (SHA-256) it rests on and the rule-specific details. For segregation-of-duties
    conflicts (A-05) each function is shown with the access path that grants it, for example
    `PAY-00042 -> R-PAY-TEAMLEAD -> R-PAY-RELEASE -> E-PAY-APPROVE`. For journal-entry
    findings the entry is shown with all its lines, the preparer and approver; for the
    Benford rules B-01 and B-02 the digit table with expected and observed proportions.
    """
    access, journal, findings = _load(data_dir, as_of, ModuleName.all)
    finding = _find_by_id_or_prefix(findings, finding_id)

    evidence = _evidence(access, journal)
    names = {ev.sha256: name for name, ev in evidence.items()}
    as_of_a = access.as_of if access is not None else None
    as_of_b = journal.as_of if journal is not None else None
    audit_dates = []
    if as_of_a is not None:
        audit_dates.append(as_of_a.isoformat())
    if as_of_b is not None and as_of_b != as_of_a:
        audit_dates.append(f"module B {as_of_b.isoformat()}")
    lines = [
        f"Finding:     {finding.finding_id}",
        f"Rule:        {finding.rule_id} v{finding.rule_version} - {_rule_title(finding.rule_id)}",
        f"Severity:    {finding.severity.name}",
        f"Entity:      {finding.entity}",
        f"Description: {finding.description}",
        f"Audit date:  {', '.join(audit_dates)}",
        "",
        "Parameters:",
        *[f"  {k}: {json.dumps(v, default=str)}" for k, v in finding.params.items()],
        "",
        "Evidence (SHA-256):",
        *[f"  {names.get(h, '?'):<18} {h}" for h in finding.evidence],
        "",
        "Access paths:" if finding.rule_id == "A-05" else "Details:",
        *_render_details(finding, access, journal),
    ]
    lines += ["", "Regulation references: " + (", ".join(finding.regulation_refs) or "none")]
    typer.echo("\n".join(lines))


def _ground_truth_report(
    truth: dict[str, Any], found: set[tuple[str, str]], expected: set[tuple[str, str]]
) -> str:
    rule_ids = _all_rule_ids(ModuleName.all, *(r for r, _ in expected | found))
    rows: list[list[str]] = []
    totals: dict[str, list[int]] = {}
    for rule_id in rule_ids:
        module = _module_of(rule_id)
        exp = {e for r, e in expected if r == rule_id}
        got = {e for r, e in found if r == rule_id}
        hits = len(exp & got)
        t = totals.setdefault(module, [0, 0, 0])
        t[0] += len(exp)
        t[1] += len(got)
        t[2] += hits
        rows.append(
            [module, rule_id, _rule_title(rule_id), str(len(exp)), str(len(got)), str(hits)]
            + [_percent(hits, len(exp)), _percent(hits, len(got))]
        )
    for module, (exp_n, got_n, hit_n) in sorted(totals.items()):
        rows.append(
            [module, f"**Total {module}**", "", str(exp_n), str(got_n), str(hit_n)]
            + [_percent(hit_n, exp_n), _percent(hit_n, got_n)]
        )
    hits = len(expected & found)
    rows.append(
        ["", "**Total**", "", str(len(expected)), str(len(found)), str(hits)]
        + [_percent(hits, len(expected)), _percent(hits, len(found))]
    )
    verdict = "PASS" if expected == found else "FAIL"
    as_of_b = truth.get("journal_as_of", truth.get("as_of"))
    out = [
        "## Ground-truth check",
        "",
        f"- Seed: {truth.get('seed')}",
        f"- As of module A: {truth.get('as_of')}",
        f"- As of module B: {as_of_b}",
        f"- Result: **{verdict}**",
        "",
        _md_table(
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


@app.command("check-ground-truth")
def check_ground_truth(data_dir: DataDir) -> None:
    """Compare the findings with the planted issues in ground_truth.json.

    Prints a GitHub-markdown report with recall (share of planted issues found) and
    precision (share of findings that were planted), grouped by module. Exit code 0 only
    if both sets are identical, so it can gate a CI pipeline.
    """
    truth = _read_ground_truth(data_dir)
    if truth is None:
        raise _fail(
            f"{data_dir / GROUND_TRUTH_FILE} not found, nothing to compare with", EXIT_USAGE
        )
    _, _, findings = _load(data_dir, None, ModuleName.all)
    found = {(f.rule_id, f.entity) for f in findings}
    expected = {(i["rule_id"], i["entity"]) for i in truth["issues"]}
    typer.echo(_ground_truth_report(truth, found, expected))
    if found != expected:
        raise typer.Exit(EXIT_FAILED)


@app.command()
def benford(
    data_dir: DataDir,
    by: Annotated[
        str,
        typer.Option("--by", help="Group the amounts by 'employee' or 'account'."),
    ] = "employee",
    min_n: Annotated[
        int | None, typer.Option("--min-n", help="Groups below this size are marked as too small.")
    ] = None,
) -> None:
    """Show the digit distributions behind the Benford rules.

    Groups by preparer employee (first digit, basis of B-01) or by GL account (first two
    digits, basis of B-02). The summary lists every group, including the ones too small to
    judge; the full digit table follows for groups that are too small or nonconforming, so
    an auditor can see why a rule fired or stayed silent.
    """
    if by not in ("employee", "account"):
        raise _fail("--by must be 'employee' or 'account'", EXIT_USAGE)
    truth = _read_ground_truth(data_dir)
    try:
        data = load_journal_data(data_dir, as_of=_resolve_b(data_dir, None, truth))
    except EvidenceError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    groups = benford_groups(data, by)  # type: ignore[arg-type]
    default_min = 1000 if by == "employee" else 5000
    threshold = default_min if min_n is None else min_n
    summary_rows: list[list[str]] = []
    details: list[tuple[str, Any]] = []
    for key, result in sorted(groups.items()):
        small = result.n < threshold
        assessment = "too small to judge" if small else result.conformity
        summary_rows.append(
            [
                key,
                str(result.n),
                f"{result.mad:.5f}",
                f"{result.chi_square:.2f}",
                assessment,
            ]
        )
        if small or "Nonconform" in result.conformity:
            details.append((key, result))
    typer.echo(
        f"Benford digit test by {by} (first digit)"
        if by == "employee"
        else "by account (first two digits)"
    )
    typer.echo("")
    typer.echo(_text_table(["group", "n", "MAD", "chi-square", "assessment"], summary_rows))
    for key, result in details:
        typer.echo("")
        typer.echo(f"{key}: digit table")
        table = result.table()
        rows = [
            [
                str(int(r.digit)),
                f"{r.expected:.4%}",
                f"{r.observed:.4%}",
                str(int(r.count)),
                f"{r.z:+.2f}",
            ]
            for r in table.itertuples(index=False)
        ]
        typer.echo(_text_table(["digit", "expected", "observed", "count", "z"], rows))


if __name__ == "__main__":  # pragma: no cover
    app()
