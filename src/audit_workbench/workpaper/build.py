"""Build the workpaper document model from one run (spec docs/design/phase-5-workpaper.md).

Every number and every name in the document comes from the run: the loaded evidence,
the rules, the findings and the sample files. What needs an auditor's judgment (the
conclusions of section 7, the reviewer fields of section 8, the result of each sampled
item) is created empty and stays empty.
"""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from audit_workbench import __version__
from audit_workbench.core.evidence import Evidence, sha256_file
from audit_workbench.core.findings import Finding, Severity
from audit_workbench.regulation import load_mapping
from audit_workbench.reporting import (
    ModuleName,
    all_rule_ids,
    collect_evidence,
    module_of,
    render_details,
    rule_class,
    rule_summary,
    rule_title,
    rule_version,
)
from audit_workbench.runner import Run, read_ground_truth
from audit_workbench.sampling import POPULATIONS, Sample
from audit_workbench.workpaper.model import (
    ISA_230_QUOTES,
    SECTION_TITLES,
    Block,
    Document,
    Heading,
    Paragraph,
    Section,
    Table,
)

DEFAULT_TITLE = "IT audit workpaper"
NOT_RECORDED = "not recorded"
_HASH_PREFIX = 12  # as in `awb run`; the full hash is in section 2
_SIGNIFICANT = (Severity.CRITICAL, Severity.HIGH)
_BENFORD = ("B-01", "B-02")
_MODULE_NAMES = {
    "A": "A (access review)",
    "B": "B (journal entries)",
    "C": "C (change management)",
    "X": "X (cross-module)",
}


class SampleMismatchError(ValueError):
    """A sample file was drawn from different data than the folder being audited."""


# --- small helpers -------------------------------------------------------------


def resolve_run_date(environ: Mapping[str, str] | None = None) -> date:
    """The date of the run: `SOURCE_DATE_EPOCH` if set (reproducible builds), else today."""
    env = os.environ if environ is None else environ
    raw = env.get("SOURCE_DATE_EPOCH")
    if raw is None or raw == "":
        return date.today()
    try:
        return datetime.fromtimestamp(int(raw), UTC).date()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"SOURCE_DATE_EPOCH must be a Unix timestamp, got {raw!r}") from exc


def audited_entity(directory: Path, truth: Mapping[str, Any] | None) -> str:
    """ "synthetic bank (seed N)" if `ground_truth.json` names a seed, else the folder name."""
    if truth is not None and truth.get("seed") is not None:
        return f"synthetic bank (seed {truth['seed']})"
    return Path(directory).resolve().name


def data_hash(evidence: Mapping[str, Evidence]) -> str:
    """One hash for all input files: SHA-256 over `<file name> <sha256>` lines, by file name."""
    lines = "".join(
        f"{ev.path.name} {ev.sha256}\n"
        for ev in sorted(evidence.values(), key=lambda e: e.path.name)
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def _lines(*items: str) -> str:
    return "\n".join(items)


def _value(value: Any) -> str:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=str)
    return str(value)


def _percent(value: Any) -> str:
    return f"{float(value) * 100:g}%"


def _key_value(rows: Sequence[tuple[str, str]], widths: tuple[float, float] = (1, 3)) -> Table:
    return Table(("Item", "Value"), tuple(rows), widths=widths)


# --- section builders ----------------------------------------------------------


def _periods(run: Run) -> str:
    lines: list[str] = []
    if run.access is not None:
        lines.append("Access (module A): state of the accounts on the audit date")
    if run.journal is not None:
        dates = sorted(
            d for d in run.journal.evidence["journal_entries"].frame["posting_date"] if d
        )
        span = f"{dates[0]} to {dates[-1]}" if dates else "no entries"
        lines.append(f"Ledger (module B): postings {span}")
    if run.change is not None:
        lines.append(
            f"Change window (module C): {run.change.period_start.isoformat()} "
            f"to {run.change.as_of.isoformat()}"
        )
    return _lines(*lines)


def _audit_dates(run: Run) -> str:
    lines: list[str] = []
    if run.access is not None:
        lines.append(f"Module A (access): {run.access.as_of.isoformat()}")
    if run.journal is not None:
        lines.append(f"Module B (journal): {run.journal.as_of.isoformat()}")
    if run.change is not None:
        lines.append(f"Module C (change): {run.change.as_of.isoformat()}")
    return _lines(*lines)


def _cover(
    run: Run,
    *,
    entity: str,
    module: ModuleName,
    min_severity: Severity,
    run_date: date,
    digest: str,
) -> Section:
    modules = [m for m in "ABCX" if module is ModuleName.all or module.value.upper() == m]
    rows = [
        ("Audited entity", entity),
        ("Audit date (as_of)", _audit_dates(run)),
        ("Periods covered", _periods(run)),
        ("Modules", ", ".join(_MODULE_NAMES[m] for m in modules)),
        ("Minimum severity listed", min_severity.name),
        ("Tool", f"audit-workbench {__version__}"),
        ("Run date", run_date.isoformat()),
        ("Data hash (SHA-256)", digest),
    ]
    quotes = {ref: quote for ref, quote in ISA_230_QUOTES}
    refs = list(quotes)
    isa = (
        "This workpaper documents one run of the audit rules. Its structure follows "
        f'ISA 230 Audit Documentation. {refs[0]} asks for documentation "{quotes[refs[0]]}" '
        "the nature, timing and extent of the procedures, their results and the significant "
        f'matters. {refs[1]} asks to record "{quotes[refs[1]]}", and {refs[2]} to record '
        f'"{quotes[refs[2]]}".'
    )
    mapping = Table(
        ("ISA 230", "Section of this workpaper", "Filled by"),
        (
            ("8(a)", "3 Procedures", "The run: rule, version, parameters, audit date, input files"),
            ("8(b)", "4 Results, 5 Findings", "The run: counts, finding records, evidence hashes"),
            (
                "8(c)",
                "7 Significant matters",
                "The auditor: one open row per CRITICAL and HIGH finding",
            ),
            (
                "9(a)",
                "2 Data basis, 5 Findings, 6 Samples (only with --sample)",
                "The run: file hashes, row counts, entity keys, sample header",
            ),
            ("9(b)", "8 Sign-off", "The preparer (--preparer) and the date of the run"),
            ("9(c)", "8 Sign-off", "The reviewer: left blank on purpose"),
        ),
        widths=(1, 3, 4),
    )
    open_parts = (
        "Section 7 (Significant matters), the reviewer fields of Section 8 and the Result "
        "column of Section 6 are to be completed by the auditor. The tool records no "
        "conclusion, no judgment and no reviewer; these parts stay empty on purpose, because "
        "a workpaper that looks reviewed but was not is worse than one with a visible gap."
    )
    return Section(
        1,
        SECTION_TITLES[1],
        (
            _key_value(rows),
            Paragraph(isa),
            mapping,
            Paragraph(open_parts),
        ),
    )


def _data_basis(evidence: Mapping[str, Evidence], digest: str) -> Section:
    rows = tuple(
        (ev.path.name, str(ev.rows), str(len(ev.frame.columns)), ev.sha256)
        for ev in sorted(evidence.values(), key=lambda e: e.path.name)
    )
    return Section(
        2,
        SECTION_TITLES[2],
        (
            Paragraph(
                "The findings rest on exactly these input files. Each file was hashed with "
                "SHA-256 before it was read, so the hash describes the bytes that were tested."
            ),
            Table(
                ("File", "Rows", "Columns", "SHA-256"),
                rows,
                widths=(3.4, 1.5, 1.5, 10),
                mono_columns=(3,),
            ),
            Paragraph(
                "Data hash of the run: SHA-256 over the lines '<file name> <sha256>' of the "
                "files above, sorted by file name, each line ending in a line feed. It is "
                "printed in the footer of every page.",
                "note",
            ),
            Paragraph(digest, "mono"),
        ),
    )


def _as_of_of(run: Run, rule_id: str) -> date | None:
    module = module_of(rule_id)
    data = {"A": run.access, "B": run.journal, "C": run.change, "X": run.access}.get(module)
    return data.as_of if data is not None else None


def _parameters(run: Run, rule_id: str) -> str:
    cls = rule_class(rule_id)
    params = dict(cls().params) if cls is not None else {}
    lines = [
        f"{key}={'built-in' if key == 'ruleset_path' and value is None else _value(value)}"
        for key, value in params.items()
    ]
    as_of = _as_of_of(run, rule_id)
    if as_of is not None:
        lines.append(f"as_of={as_of.isoformat()}")
    return _lines(*lines)


def _references(rule_id: str) -> str:
    refs = load_mapping().get(rule_id, ())
    return "\n".join(f'{ref}\n"{ref.quote}"' for ref in refs) or "none"


def _procedures(run: Run, module: ModuleName) -> Section:
    rows = tuple(
        (
            rule_id,
            rule_version(rule_id),
            _lines(rule_title(rule_id), rule_summary(rule_id)),
            _parameters(run, rule_id),
            _references(rule_id),
        )
        for rule_id in all_rule_ids(module)
    )
    return Section(
        3,
        SECTION_TITLES[3],
        (
            Paragraph(
                "Timing: every test is relative to the audit date of its module (section 1), "
                "never to the clock. Extent: each rule ran over the complete input files of "
                "section 2; none of the rules is sample-based. Parameters are the values the "
                "rule ran with."
            ),
            Table(
                (
                    "Rule",
                    "Version",
                    "Title and what it tests",
                    "Parameters",
                    "Regulation references",
                ),
                rows,
                widths=(1.1, 1.5, 4.2, 3.2, 6),
                mono_columns=(0, 1),
            ),
        ),
    )


def _results(
    findings: Sequence[Finding], listed: int, module: ModuleName, level: Severity
) -> Section:
    per_rule = Counter(f.rule_id for f in findings)
    per_cell = Counter((f.rule_id, f.severity) for f in findings)
    severities = (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)
    rows: list[tuple[str, ...]] = []
    for rule_id in all_rule_ids(module, *per_rule):
        total = per_rule[rule_id]
        rows.append(
            (
                rule_id,
                rule_title(rule_id),
                *(str(per_cell[(rule_id, s)]) for s in severities),
                str(total),
                f"{total} exception{'s' if total != 1 else ''}"
                if total
                else "tested, no exception",
            )
        )
    rows.append(
        (
            "Total",
            "",
            *(str(sum(1 for f in findings if f.severity is s)) for s in severities),
            str(len(findings)),
            "",
        )
    )
    total = len(findings)
    hidden = total - listed
    if total == 0:
        note = "The rules raised no findings: every rule was tested and none found an exception."
    elif hidden:
        note = (
            f"{hidden} of {total} findings are below the minimum severity ({level.name}) "
            "and are not listed in section 5. They are counted here."
        )
    else:
        note = f"All {total} findings are listed in section 5."
    return Section(
        4,
        SECTION_TITLES[4],
        (
            Table(
                ("Rule", "Title", "Low", "Medium", "High", "Critical", "Total", "Result"),
                tuple(rows),
                widths=(1.2, 5.6, 1.1, 1.4, 1.1, 1.4, 1.1, 3),
                mono_columns=(0,),
            ),
            Paragraph(note),
        ),
    )


def _short_details(finding: Finding, run: Run) -> str:
    """The rule-specific details in short form: access path, journal entry, change record."""
    if finding.rule_id in _BENFORD:
        summary = render_details(finding, run.access, run.journal, run.change)[-1].strip()
        lines = [summary, f"Digit table: awb explain {finding.finding_id}"]
    else:
        lines = render_details(finding, run.access, run.journal, run.change)
    text = textwrap.dedent("\n".join(lines)).strip("\n")
    return "Details:\n" + "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def _evidence_cell(finding: Finding, names: Mapping[str, str]) -> str:
    return _lines(*(f"{names.get(h, '?')}  {h[:_HASH_PREFIX]}" for h in finding.evidence))


def _findings(
    listed: Sequence[Finding], run: Run, evidence: Mapping[str, Evidence], total: int
) -> Section:
    names = {ev.sha256: ev.path.name for ev in evidence.values()}
    blocks: list[Block] = [
        Paragraph(
            "Most severe first. Evidence shows the file name and the first "
            f"{_HASH_PREFIX} characters of its SHA-256; the full hash is in section 2.",
            "note",
        )
    ]
    if not listed:
        blocks.append(
            Paragraph(
                "No findings."
                if total == 0
                else "No finding reaches the minimum severity; the findings are counted in section 4."
            )
        )
    for finding in listed:
        blocks.append(Heading(f"{finding.finding_id}  {finding.rule_id}  {finding.severity.name}"))
        blocks.append(
            Table(
                ("Field", "Value"),
                (
                    (
                        "Rule",
                        f"{finding.rule_id} v{finding.rule_version} - {rule_title(finding.rule_id)}",
                    ),
                    ("Severity", finding.severity.name),
                    ("Entity", finding.entity),
                    ("Description", finding.description),
                    ("Evidence", _evidence_cell(finding, names)),
                    ("Regulation", _lines(*finding.regulation_refs) or "none"),
                ),
                widths=(1.3, 5),
            )
        )
        blocks.append(Paragraph(_short_details(finding, run), "mono"))
    return Section(5, SECTION_TITLES[5], tuple(blocks))


def verify_sample(sample: Sample, directory: Path) -> None:
    """Raise unless the sample was drawn from the file as it is in `directory`."""
    population = POPULATIONS.get(sample.population)
    if population is None:
        raise ValueError(f"unknown population {sample.population!r} in the sample file")
    path = Path(directory) / population.file
    if not path.is_file():
        raise SampleMismatchError(f"{population.file} not found in {directory}")
    if sha256_file(path) != sample.sha256:
        raise SampleMismatchError(
            f"the sample of {sample.population} was drawn from different data: "
            f"{population.file} has another SHA-256 than the sample header"
        )


def _samples(samples: Sequence[Sample]) -> Section:
    blocks: list[Block] = [
        Paragraph(
            "Each sample was drawn from the population file shown, whose SHA-256 was "
            "checked against the sample header. The auditor records the result for each "
            "item.",
            "note",
        )
    ]
    for number, sample in enumerate(samples, start=1):
        params = sample.parameters

        def get(key: str, fmt=_value, params=params) -> str:
            return fmt(params[key]) if key in params else NOT_RECORDED

        blocks.append(Heading(f"Sample {number}: {sample.population}"))
        blocks.append(
            _key_value(
                [
                    ("Population", f"{sample.population} ({POPULATIONS[sample.population].file})"),
                    ("Population hash (SHA-256)", sample.sha256),
                    ("Population size (N)", str(sample.population_size)),
                    ("Sample size (n)", str(sample.n)),
                    ("Replacement items", str(sample.replacements)),
                    ("Method", get("method")),
                    ("Confidence", get("confidence", _percent)),
                    ("Tolerable deviation rate", get("tolerable_rate", _percent)),
                    ("Expected deviation rate", get("expected_rate", _percent)),
                    ("Deviations allowed", get("deviations_allowed")),
                    ("Seed", str(sample.seed)),
                ]
            )
        )
        blocks.append(
            Table(
                ("Rank", "Key", "Role", "Result"),
                tuple(
                    (str(i.rank), i.key, "replacement" if i.replacement else "sample", "")
                    for i in sample.items
                ),
                widths=(1, 4, 2, 6),
                mono_columns=(1,),
            )
        )
    return Section(6, SECTION_TITLES[6], tuple(blocks))


def _significant(listed: Sequence[Finding]) -> Section:
    significant = [f for f in listed if f.severity in _SIGNIFICANT]
    intro = Paragraph(
        "This section is to be completed by the auditor (ISA 230.8(c)). The tool lists the "
        "CRITICAL and HIGH findings of section 5 and leaves the columns for the discussion, "
        "the conclusion and the professional judgment empty."
    )
    if not significant:
        return Section(
            7,
            SECTION_TITLES[7],
            (
                intro,
                Paragraph(
                    "No CRITICAL or HIGH finding is listed in section 5, so there is no row to "
                    "complete. The auditor records any other significant matter here."
                ),
            ),
        )
    rows = tuple(
        (
            f.finding_id,
            _lines(f.rule_id, f.severity.name, f.entity),
            "",
            "",
            "",
        )
        for f in significant
    )
    return Section(
        7,
        SECTION_TITLES[7],
        (
            intro,
            Table(
                (
                    "Finding",
                    "Rule, severity, entity",
                    "Discussed with / when",
                    "Conclusion",
                    "Judgment",
                ),
                rows,
                widths=(2.6, 3.2, 3.7, 3.7, 3.7),
                mono_columns=(0,),
            ),
        ),
    )


def _sign_off(preparer: str, run_date: date) -> Section:
    return Section(
        8,
        SECTION_TITLES[8],
        (
            _key_value(
                [
                    ("Prepared by", preparer.strip() or NOT_RECORDED),
                    ("Date of the run (work completed)", run_date.isoformat()),
                    ("Reviewed by", ""),
                    ("Date of review", ""),
                    ("Extent of review", ""),
                ]
            ),
            Paragraph(
                "The reviewer fields are empty on purpose. They are completed by the reviewer "
                "(ISA 230.9(c)), not by the tool.",
                "note",
            ),
        ),
    )


# --- the document --------------------------------------------------------------


def build_document(
    run: Run,
    *,
    directory: Path,
    module: ModuleName,
    run_date: date,
    min_severity: Severity = Severity.LOW,
    samples: Sequence[Sample] = (),
    preparer: str = "",
    title: str = DEFAULT_TITLE,
) -> Document:
    """The workpaper of one run. `run.findings` are all findings, before the severity filter."""
    evidence = collect_evidence(run.access, run.journal, run.change)
    digest = data_hash(evidence)
    entity = audited_entity(directory, read_ground_truth(Path(directory)))
    listed = sorted(
        (f for f in run.findings if f.severity >= min_severity),
        key=lambda f: (-f.severity, f.rule_id, f.entity),
    )
    sections = [
        _cover(
            run,
            entity=entity,
            module=module,
            min_severity=min_severity,
            run_date=run_date,
            digest=digest,
        ),
        _data_basis(evidence, digest),
        _procedures(run, module),
        _results(run.findings, len(listed), module, min_severity),
        _findings(listed, run, evidence, len(run.findings)),
    ]
    if samples:
        sections.append(_samples(samples))
    sections += [_significant(listed), _sign_off(preparer, run_date)]
    return Document(
        title=title,
        sections=tuple(sections),
        data_hash=digest,
        run_date=run_date,
        tool=f"audit-workbench {__version__}",
        author=preparer.strip(),
    )
