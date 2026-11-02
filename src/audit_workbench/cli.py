"""The `awb` command line: run the audit tests and check them against ground truth.

Typical audit workflow:

1. `awb generate`            build a synthetic bank (or point the tools at your own extract),
2. `awb run`                 execute the access, journal-entry and change rules and list the
                             exceptions,
3. `awb explain`             drill into one exception: rule, parameters, evidence, access path,
                             the journal entry with its lines, or the change with its approvals,
                             commits and deployments,
4. `awb benford`             look at the digit distributions behind the Benford findings,
5. `awb workpaper`           write the run as an audit workpaper (DOCX or PDF, ISA 230 layout),
6. `awb check-ground-truth`  prove the rules find exactly the planted issues (used in CI).

Every command is deterministic: same files and same audit date give the same findings
and the same finding ids. Module A is the access review, module B the journal-entry
testing, module C the change management.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.change.data import ChangeData, load_change_evidence
from audit_workbench.core.evidence import Evidence, EvidenceError
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.journal.anomaly import rank_entries
from audit_workbench.journal.benford import benford_groups
from audit_workbench.journal.data import JournalData, load_journal_data
from audit_workbench.reporting import (
    ModuleName,
    all_rule_ids,
    collect_evidence,
    module_of,
    render_details,
    rule_title,
    sorted_findings,
    text_table,
)
from audit_workbench.runner import (
    GROUND_TRUTH_FILE,
    RunError,
    load_run,
    read_ground_truth,
    resolve_a,
    resolve_b,
)
from audit_workbench.sampling import (
    POPULATIONS,
    SelectionDates,
    evaluate,
    plan_sample,
    read_sample,
    sample_size,
    write_sample,
)
from audit_workbench.synth import generate_bank
from audit_workbench.workpaper.build import (
    DEFAULT_TITLE,
    SampleMismatchError,
    build_document,
    resolve_run_date,
    verify_sample,
)

EXIT_FAILED = 1  # the check ran and found a problem (unknown finding, ground-truth mismatch)
EXIT_USAGE = 2  # the command could not run (missing input, missing audit date)
_DESCRIPTION_WIDTH = 70

app = typer.Typer(
    name="awb",
    help="Audit workbench: access review, journal-entry and change management tests on bank IT data.",
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


class SampleMethod(StrEnum):
    binomial = "binomial"
    hypergeometric = "hypergeometric"


# --- helpers -----------------------------------------------------------------


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _read_ground_truth(directory: Path) -> dict[str, Any] | None:
    return read_ground_truth(directory)


def _resolve_a(directory: Path, as_of: datetime | None, truth: dict[str, Any] | None) -> date:
    """`--as-of` wins; otherwise take the access-review date from ground_truth.json."""
    try:
        return resolve_a(directory, as_of.date() if as_of else None, truth)
    except RunError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc


def _resolve_b(directory: Path, as_of: datetime | None, truth: dict[str, Any] | None) -> date:
    """`--as-of` wins; otherwise the journal date (`journal_as_of`) or the bank's `as_of`."""
    try:
        return resolve_b(directory, as_of.date() if as_of else None, truth)
    except RunError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc


def _load(
    directory: Path, as_of: datetime | None, module: ModuleName
) -> tuple[AccessData | None, JournalData | None, ChangeData | None, list[Finding]]:
    """Load the evidence the selected modules need and run their rules."""
    try:
        run = load_run(directory, as_of.date() if as_of else None, module)
    except RunError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    return run.access, run.journal, run.change, run.findings


def _short(text: str, width: int = _DESCRIPTION_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _percent(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{100 * numerator / denominator:.1f}%"


# --- run: renderers ----------------------------------------------------------


def _render_table(
    findings: list[Finding],
    evidence: dict[str, Evidence],
    as_of_a: date | None,
    as_of_b: date | None,
    as_of_c: date | None,
    module: ModuleName,
) -> str:
    out: list[str] = []
    if as_of_a is not None:
        out.append(f"Audit date (as_of): {as_of_a.isoformat()}")
    if as_of_b is not None:
        out.append(f"Journal as_of: {as_of_b.isoformat()}")
    if as_of_c is not None:
        out.append(f"Change as_of: {as_of_c.isoformat()}")
    out.append("")
    if findings:
        rows = [
            [
                f.finding_id,
                module_of(f.rule_id),
                f.rule_id,
                f.severity.name,
                f.entity,
                _short(f.description),
            ]
            for f in findings
        ]
        out.append(
            text_table(["finding_id", "module", "rule", "severity", "entity", "description"], rows)
        )
    else:
        out.append("No findings.")
    counts = Counter(f.rule_id for f in findings)
    out += ["", "Findings per rule:"]
    out.append(
        text_table(
            ["module", "rule", "title", "count"],
            [
                [module_of(r), r, rule_title(r), str(counts[r])]
                for r in all_rule_ids(module, *counts)
            ]
            + [["", "Total", "", str(len(findings))]],
        )
    )
    out += ["", "Input files (SHA-256, first 12 characters):"]
    out.append(
        text_table(
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
    return text_table(["rank", "score", "doc_no", "preparer"], rows)


def _render_json(findings: list[Finding]) -> str:
    return json.dumps([f.model_dump(mode="json") for f in findings], indent=2)


def _render_markdown(findings: list[Finding]) -> str:
    if not findings:
        return "No findings."
    rows = [
        [f.finding_id, module_of(f.rule_id), f.rule_id, f.severity.name, f.entity, f.description]
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
    that the rules stay silent on healthy data. The bank contains the access data of module A,
    the journal ledger of module B and the change records of module C.
    """
    generate_bank(out_dir, seed=seed, inject=not clean)
    evidence = load_access_evidence(out_dir)
    change_evidence = load_change_evidence(out_dir)
    issues = _read_ground_truth(out_dir)["issues"]  # type: ignore[index]
    per_module = Counter(module_of(i["rule_id"]) for i in issues)
    typer.echo(
        f"{out_dir}: {evidence['hr_employees'].rows} employees, "
        f"{evidence['accounts'].rows} accounts, "
        f"{change_evidence['changes'].rows} changes, "
        f"{change_evidence['deployments'].rows} deployments, "
        f"{len(issues)} injected issues "
        f"(A {per_module['A']}, B {per_module['B']}, C {per_module['C']}, X {per_module['X']})"
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
        ModuleName,
        typer.Option(
            "--module", help="Run module A (access), B (journal), C (change), X (cross) or all."
        ),
    ] = ModuleName.all,
    rank: Annotated[
        int | None,
        typer.Option("--rank", help="Append the top-N anomaly ranking as a selection aid."),
    ] = None,
) -> None:
    """Run the audit rules and list the exceptions, most severe first.

    By default module A (access review), module B (journal-entry testing), module C
    (change management) and module X (cross-module cases) run together.
    The table ends with finding counts per rule and the SHA-256 prefix of every input file,
    so the run can be tied to the exact data it tested. Use --format json for the full
    finding records (ids, parameters, evidence hashes) and --format markdown for a report.
    --rank appends the anomaly ranking of manual journal entries; it is a sampling aid for
    choosing entries to inspect and deliberately does not produce findings.
    """
    access, journal, change, findings = _load(data_dir, as_of, module)
    threshold = Severity[min_severity.name.upper()]
    findings = sorted_findings([f for f in findings if f.severity >= threshold])

    if rank is not None:
        if output_format is OutputFormat.json:
            raise _fail("--rank cannot be combined with --format json", EXIT_USAGE)
        if journal is None:
            raise _fail("--rank needs module B data (--module b or --module all)", EXIT_USAGE)

    as_of_a = access.as_of if access is not None else None
    as_of_b = journal.as_of if journal is not None else None
    as_of_c = change.as_of if change is not None else None

    if output_format is OutputFormat.json:
        text = _render_json(findings)
    elif output_format is OutputFormat.markdown:
        text = _render_markdown(findings)
        if rank is not None and journal is not None:
            text += "\n\n### Anomaly ranking (selection aid, not findings)\n\n" + _render_rank(
                journal, rank
            )
    else:
        text = _render_table(
            findings, collect_evidence(access, journal, change), as_of_a, as_of_b, as_of_c, module
        )
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
    Benford rules B-01 and B-02 the digit table with expected and observed proportions. For
    cross-module findings (module X) the journal entries the case rests on, for change
    findings (module C) the change is shown with its approvals in time order, its
    linked commits with their authors and every production deployment with the deploying
    employee and the account used; a deployment without a change is shown on its own.
    """
    access, journal, change, findings = _load(data_dir, as_of, ModuleName.all)
    finding = _find_by_id_or_prefix(findings, finding_id)

    evidence = collect_evidence(access, journal, change)
    names = {ev.sha256: name for name, ev in evidence.items()}
    as_of_a = access.as_of if access is not None else None
    as_of_b = journal.as_of if journal is not None else None
    audit_dates = []
    if as_of_a is not None:
        audit_dates.append(as_of_a.isoformat())
    if as_of_b is not None and as_of_b != as_of_a:
        audit_dates.append(f"module B {as_of_b.isoformat()}")
    as_of_c = change.as_of if change is not None else None
    if as_of_c is not None and as_of_c != as_of_a:
        audit_dates.append(f"module C {as_of_c.isoformat()}")
    lines = [
        f"Finding:     {finding.finding_id}",
        f"Rule:        {finding.rule_id} v{finding.rule_version} - {rule_title(finding.rule_id)}",
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
        *render_details(finding, access, journal, change),
    ]
    lines += ["", "Regulation references: " + (", ".join(finding.regulation_refs) or "none")]
    typer.echo("\n".join(lines))


def _ground_truth_report(
    truth: dict[str, Any], found: set[tuple[str, str]], expected: set[tuple[str, str]]
) -> str:
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
    _, _, _, findings = _load(data_dir, None, ModuleName.all)
    found = {(f.rule_id, f.entity) for f in findings}
    expected = {(i["rule_id"], i["entity"]) for i in truth["issues"]}
    typer.echo(_ground_truth_report(truth, found, expected))
    if found != expected:
        raise typer.Exit(EXIT_FAILED)


@app.command()
def sample(
    data_dir: DataDir,
    population: Annotated[
        str,
        typer.Option("--population", help="Population to sample; see the command help."),
    ],
    confidence: Annotated[float, typer.Option("--confidence", help="Confidence level.")] = 0.95,
    tolerable: Annotated[
        float, typer.Option("--tolerable", help="Tolerable deviation rate.")
    ] = 0.05,
    expected: Annotated[float, typer.Option("--expected", help="Expected deviation rate.")] = 0.01,
    seed: Annotated[int, typer.Option("--seed", help="Random seed; same seed, same sample.")] = 1,
    method: Annotated[
        SampleMethod, typer.Option("--method", help="Sample-size method.")
    ] = SampleMethod.binomial,
    replacements: Annotated[
        int, typer.Option("--replacements", help="Replacement items to draw on top.")
    ] = 5,
    as_of: AsOfOption = None,
    period_start: Annotated[
        datetime | None,
        typer.Option("--period-start", formats=["%Y-%m-%d"], help="Period start (YYYY-MM-DD)."),
    ] = None,
    period_end: Annotated[
        datetime | None,
        typer.Option("--period-end", formats=["%Y-%m-%d"], help="Period end (YYYY-MM-DD)."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Write the sample file here.")
    ] = None,
) -> None:
    """Draw an attribute sample from one population and write it as a file.

    The sample size comes from the confidence, the tolerable deviation rate and the
    expected deviation rate; the items come from a seeded random permutation, so the same
    population, parameters and seed always give the same sample. The file starts with the
    population, the input file SHA-256, the population size, the sample size, the seed and
    all parameters, so a sample can be reproduced and defended later.

    Populations: changes, deployments, manual_entries, recert_decisions,
    active_personal_accounts. The period defaults to the twelve months ending at --as-of.
    """
    if population not in POPULATIONS:
        known = ", ".join(sorted(POPULATIONS))
        raise _fail(f"unknown population {population!r}, expected one of: {known}", EXIT_USAGE)
    truth = _read_ground_truth(data_dir)
    end = period_end.date() if period_end is not None else _resolve_a(data_dir, as_of, truth)
    start = period_start.date() if period_start is not None else end.replace(year=end.year - 1)
    dates = SelectionDates(as_of=end, period_start=start, period_end=end)
    try:
        drawn = plan_sample(
            data_dir,
            population,
            confidence=confidence,
            tolerable_rate=tolerable,
            expected_rate=expected,
            method=method.value,
            seed=seed,
            dates=dates,
            replacements=replacements,
        )
    except (KeyError, ValueError) as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    target = output or data_dir / f"sample_{population}_seed{seed}.csv"
    write_sample(drawn, target)
    typer.echo(
        "\n".join(
            [
                f"population:      {drawn.population} ({drawn.file})",
                f"file sha256:     {drawn.sha256}",
                f"population size: {drawn.population_size}",
                f"sample size:     {drawn.n}",
                f"replacements:    {drawn.replacements}",
                f"seed:            {drawn.seed}",
                *[f"{key + ':':<16} {value}" for key, value in sorted(drawn.parameters.items())],
                "",
                f"wrote {len(drawn.items)} rows to {target}",
            ]
        )
    )


@app.command("sample-size")
def sample_size_command(
    confidence: Annotated[float, typer.Option("--confidence", help="Confidence level.")] = 0.95,
    tolerable: Annotated[
        float, typer.Option("--tolerable", help="Tolerable deviation rate.")
    ] = 0.05,
    expected: Annotated[float, typer.Option("--expected", help="Expected deviation rate.")] = 0.01,
    method: Annotated[
        SampleMethod, typer.Option("--method", help="Sample-size method.")
    ] = SampleMethod.binomial,
    population: Annotated[
        int | None, typer.Option("--population", help="Population size for the hypergeometric.")
    ] = None,
    deviations: Annotated[
        int | None, typer.Option("--deviations", help="Deviations found, to evaluate a sample.")
    ] = None,
    n: Annotated[int | None, typer.Option("--n", help="Sample size, to evaluate a sample.")] = None,
) -> None:
    """The sample size for given parameters, without drawing anything.

    With --n and --deviations the one-sided upper deviation limit of an evaluated sample is
    shown and compared with the tolerable rate. The hypergeometric method needs --population.
    """
    try:
        size = sample_size(
            confidence,
            tolerable,
            expected,
            method=method.value,
            population=population if method is SampleMethod.hypergeometric else None,
        )
    except ValueError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    lines = [
        f"method:              {size.method}",
        f"confidence:          {size.confidence:.0%}",
        f"tolerable rate:      {size.tolerable_rate:.2%}",
        f"expected rate:       {size.expected_rate:.2%}",
        f"population size:     {size.population if size.population is not None else '-'}",
        f"sample size:         {size.n}",
        f"deviations allowed:  {size.deviations_allowed}",
        f"full population:     {'yes' if size.full_population else 'no'}",
    ]
    if n is not None and deviations is not None:
        try:
            result = evaluate(n, deviations, confidence, tolerable)
        except ValueError as exc:
            raise _fail(str(exc), EXIT_USAGE) from exc
        lines += [
            "",
            f"evaluated sample:    n={n}, deviations={deviations}",
            f"upper deviation limit: {result.upper_limit:.2%}",
            f"within tolerable rate: {'yes' if result.within_tolerable else 'no'}",
        ]
    typer.echo("\n".join(lines))


WORKPAPER_FORMATS = (".docx", ".pdf")
WORKPAPER_EXTRA_HINT = (
    "awb workpaper needs the optional extra 'workpaper' (python-docx and reportlab). "
    "Install it with: uv sync --extra workpaper  or  pip install 'audit-workbench[workpaper]'"
)


@app.command()
def workpaper(
    data_dir: DataDir,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Output file; the format follows the suffix (.docx or .pdf). "
            "Default: DATA_DIR/workpaper.docx.",
        ),
    ] = None,
    as_of: AsOfOption = None,
    module: Annotated[
        ModuleName,
        typer.Option(
            "--module", help="Run module A (access), B (journal), C (change), X (cross) or all."
        ),
    ] = ModuleName.all,
    min_severity: Annotated[
        SeverityName, typer.Option(help="List only findings at or above this severity.")
    ] = SeverityName.low,
    sample: Annotated[
        list[Path] | None,
        typer.Option(
            "--sample",
            help="A sample file written by `awb sample`; repeat for several. Each becomes a "
            "part of section 6.",
        ),
    ] = None,
    preparer: Annotated[
        str, typer.Option("--preparer", help="Name of the person who ran the work.")
    ] = "",
    title: Annotated[str, typer.Option("--title", help="Title of the workpaper.")] = DEFAULT_TITLE,
) -> None:
    """Write one run as an audit workpaper in DOCX or PDF.

    The structure follows ISA 230 (paras. 8 and 9): cover, data basis with the SHA-256 of
    every input file, procedures (rule, version, parameters, regulation references),
    results per rule and severity, the findings with their evidence, the samples given
    with --sample, significant matters and sign-off. Every number comes from the run.

    The parts that need an auditor's judgment stay empty: the conclusions of section 7,
    the result of each sampled item and the reviewer fields of section 8. Without
    --preparer the preparer is shown as "not recorded". The extra 'workpaper' must be
    installed (uv sync --extra workpaper). SOURCE_DATE_EPOCH fixes the run date, so the
    same data gives the same text.
    """
    target = output or data_dir / "workpaper.docx"
    suffix = target.suffix.lower()
    if suffix not in WORKPAPER_FORMATS:
        raise _fail(
            f"unsupported output format {target.suffix!r}, use a .docx or .pdf file", EXIT_USAGE
        )
    try:
        if suffix == ".docx":
            from audit_workbench.workpaper.render_docx import render_docx as render
        else:
            from audit_workbench.workpaper.render_pdf import render_pdf as render
    except ImportError as exc:
        raise _fail(f"{WORKPAPER_EXTRA_HINT} ({exc})", EXIT_USAGE) from exc
    try:
        run_date = resolve_run_date()
        samples = [read_sample(path) for path in sample or []]
        for drawn in samples:
            verify_sample(drawn, data_dir)
    except SampleMismatchError as exc:
        raise _fail(str(exc), EXIT_FAILED) from exc
    except ValueError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc

    try:
        run = load_run(data_dir, as_of.date() if as_of else None, module)
    except RunError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    threshold = Severity[min_severity.name.upper()]
    document = build_document(
        run,
        directory=data_dir,
        module=module,
        run_date=run_date,
        min_severity=threshold,
        samples=samples,
        preparer=preparer,
        title=title,
    )
    render(document, target)
    listed = sum(1 for f in run.findings if f.severity >= threshold)
    typer.echo(
        f"wrote {target}: {listed} of {len(run.findings)} findings listed, "
        f"{len(samples)} sample(s), data hash {document.data_hash[:12]}"
    )


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
    typer.echo(text_table(["group", "n", "MAD", "chi-square", "assessment"], summary_rows))
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
        typer.echo(text_table(["digit", "expected", "observed", "count", "z"], rows))


if __name__ == "__main__":  # pragma: no cover
    app()
