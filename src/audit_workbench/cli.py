"""The `awb` command line: run the access-review tests and check them against ground truth.

Typical audit workflow:

1. `awb generate`            build a synthetic bank (or point the tools at your own extract),
2. `awb run`                 execute all access rules and list the exceptions,
3. `awb explain`             drill into one exception: rule, parameters, evidence, access path,
4. `awb check-ground-truth`  prove the rules find exactly the planted issues (used in CI).

Every command is deterministic: same files and same audit date give the same findings
and the same finding ids.
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
from audit_workbench.access.rules import ACCESS_RULES, run_access_rules
from audit_workbench.core.evidence import Evidence, EvidenceError
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.synth import generate_bank

GROUND_TRUTH_FILE = "ground_truth.json"
EXIT_FAILED = 1  # the check ran and found a problem (unknown finding, ground-truth mismatch)
EXIT_USAGE = 2  # the command could not run (missing input, missing audit date)
_DESCRIPTION_WIDTH = 70

app = typer.Typer(
    name="awb",
    help="Audit workbench: access review and segregation-of-duties tests on bank IT data.",
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
        help="Audit reference date (YYYY-MM-DD). Default: as_of from ground_truth.json.",
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


# --- helpers -----------------------------------------------------------------


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def _read_ground_truth(directory: Path) -> dict[str, Any] | None:
    path = directory / GROUND_TRUTH_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_as_of(directory: Path, as_of: datetime | None) -> date:
    """`--as-of` wins; otherwise take the date the bank was generated for."""
    if as_of is not None:
        return as_of.date()
    truth = _read_ground_truth(directory)
    if truth is None or "as_of" not in truth:
        raise _fail(
            f"--as-of is required because {directory / GROUND_TRUTH_FILE} does not exist",
            EXIT_USAGE,
        )
    return date.fromisoformat(truth["as_of"])


def _load(directory: Path, as_of: date) -> tuple[AccessData, list[Finding]]:
    try:
        data = AccessData(evidence=load_access_evidence(directory), as_of=as_of)
    except EvidenceError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    return data, run_access_rules(data)


def _sorted(findings: list[Finding]) -> list[Finding]:
    """Most severe first, then by rule and entity, so output is stable and diffable."""
    return sorted(findings, key=lambda f: (-f.severity, f.rule_id, f.entity))


def _rule_title(rule_id: str) -> str:
    return ACCESS_RULES.get(rule_id).title


def _all_rule_ids(*extra: str) -> list[str]:
    return sorted({*ACCESS_RULES.ids(), *extra})


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


# --- run: renderers ----------------------------------------------------------


def _render_table(findings: list[Finding], evidence: dict[str, Evidence], as_of: date) -> str:
    out = [f"Audit date (as_of): {as_of.isoformat()}", ""]
    if findings:
        rows = [
            [f.finding_id, f.rule_id, f.severity.name, f.entity, _short(f.description)]
            for f in findings
        ]
        out.append(_text_table(["finding_id", "rule", "severity", "entity", "description"], rows))
    else:
        out.append("No findings.")
    counts = Counter(f.rule_id for f in findings)
    out += ["", "Findings per rule:"]
    out.append(
        _text_table(
            ["rule", "title", "count"],
            [[r, _rule_title(r), str(counts[r])] for r in _all_rule_ids(*counts)]
            + [["Total", "", str(len(findings))]],
        )
    )
    out += ["", "Input files (SHA-256, first 12 characters):"]
    out.append(
        _text_table(
            ["file", "sha256", "rows"],
            [[ev.path.name, ev.sha256[:12], str(ev.rows)] for ev in evidence.values()],
        )
    )
    return "\n".join(out)


def _render_json(findings: list[Finding]) -> str:
    return json.dumps([f.model_dump(mode="json") for f in findings], indent=2)


def _render_markdown(findings: list[Finding]) -> str:
    if not findings:
        return "No findings."
    rows = [[f.finding_id, f.rule_id, f.severity.name, f.entity, f.description] for f in findings]
    return _md_table(["finding_id", "rule", "severity", "entity", "description"], rows)


# --- commands ----------------------------------------------------------------


@app.command()
def generate(
    out_dir: Annotated[Path, typer.Argument(help="Folder to write the CSVs into (created).")],
    seed: Annotated[int, typer.Option(help="Random seed; same seed gives identical bytes.")] = 7,
    clean: Annotated[bool, typer.Option(help="Write a bank without injected issues.")] = False,
) -> None:
    """Create a synthetic bank with planted issues and a ground_truth.json.

    Use this to get reproducible test data. With --clean no issues are planted, which shows
    that the rules stay silent on healthy data.
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
) -> None:
    """Run all access rules and list the exceptions, most severe first.

    The table ends with finding counts per rule and the SHA-256 prefix of every input file,
    so the run can be tied to the exact data it tested. Use --format json for the full
    finding records (ids, parameters, evidence hashes) and --format markdown for a report.
    """
    audit_date = _resolve_as_of(data_dir, as_of)
    data, findings = _load(data_dir, audit_date)
    threshold = Severity[min_severity.name.upper()]
    findings = _sorted([f for f in findings if f.severity >= threshold])

    if output_format is OutputFormat.json:
        text = _render_json(findings)
    elif output_format is OutputFormat.markdown:
        text = _render_markdown(findings)
    else:
        text = _render_table(findings, dict(data.evidence), audit_date)

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


def _render_details(finding: Finding) -> list[str]:
    """A-05 stores one access path per function; show it as a chain of nodes."""
    if finding.rule_id == "A-05":
        return [f"  {fn}: {' -> '.join(path)}" for fn, path in finding.details.items()]
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
    `PAY-00042 -> R-PAY-TEAMLEAD -> R-PAY-RELEASE -> E-PAY-APPROVE`.
    """
    audit_date = _resolve_as_of(data_dir, as_of)
    data, findings = _load(data_dir, audit_date)
    finding = _find_by_id_or_prefix(findings, finding_id)

    names = {ev.sha256: name for name, ev in data.evidence.items()}
    lines = [
        f"Finding:     {finding.finding_id}",
        f"Rule:        {finding.rule_id} v{finding.rule_version} - {_rule_title(finding.rule_id)}",
        f"Severity:    {finding.severity.name}",
        f"Entity:      {finding.entity}",
        f"Description: {finding.description}",
        f"Audit date:  {audit_date.isoformat()}",
        "",
        "Parameters:",
        *[f"  {k}: {json.dumps(v, default=str)}" for k, v in finding.params.items()],
        "",
        "Evidence (SHA-256):",
        *[f"  {names.get(h, '?'):<18} {h}" for h in finding.evidence],
        "",
        "Access paths:" if finding.rule_id == "A-05" else "Details:",
        *_render_details(finding),
    ]
    lines += ["", "Regulation references: " + (", ".join(finding.regulation_refs) or "none")]
    typer.echo("\n".join(lines))


def _ground_truth_report(
    truth: dict[str, Any], found: set[tuple[str, str]], expected: set[tuple[str, str]]
) -> str:
    rule_ids = _all_rule_ids(*(r for r, _ in expected | found))
    rows: list[list[str]] = []
    for rule_id in rule_ids:
        exp = {e for r, e in expected if r == rule_id}
        got = {e for r, e in found if r == rule_id}
        hits = len(exp & got)
        title = _rule_title(rule_id) if rule_id in ACCESS_RULES.ids() else "(unknown rule)"
        rows.append(
            [rule_id, title, str(len(exp)), str(len(got)), str(hits)]
            + [_percent(hits, len(exp)), _percent(hits, len(got))]
        )
    hits = len(expected & found)
    rows.append(
        ["**Total**", "", str(len(expected)), str(len(found)), str(hits)]
        + [_percent(hits, len(expected)), _percent(hits, len(found))]
    )
    verdict = "PASS" if expected == found else "FAIL"
    out = [
        "## Ground-truth check",
        "",
        f"- Seed: {truth.get('seed')}",
        f"- As of: {truth['as_of']}",
        f"- Result: **{verdict}**",
        "",
        _md_table(
            ["rule", "title", "expected", "found", "true positives", "recall", "precision"], rows
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
    precision (share of findings that were planted) per rule. Exit code 0 only if both
    sets are identical, so it can gate a CI pipeline.
    """
    truth = _read_ground_truth(data_dir)
    if truth is None:
        raise _fail(
            f"{data_dir / GROUND_TRUTH_FILE} not found, nothing to compare with", EXIT_USAGE
        )
    _, findings = _load(data_dir, date.fromisoformat(truth["as_of"]))
    found = {(f.rule_id, f.entity) for f in findings}
    expected = {(i["rule_id"], i["entity"]) for i in truth["issues"]}
    typer.echo(_ground_truth_report(truth, found, expected))
    if found != expected:
        raise typer.Exit(EXIT_FAILED)


if __name__ == "__main__":  # pragma: no cover
    app()
