"""Run the audit rules on one data folder.

The command line and the workpaper both start from the same run: resolve the audit
dates, load the evidence the selected modules need and execute their rules. Errors that
mean "the run could not be made" (missing audit date, unusable input file) are raised
as `RunError`; the caller decides how to report them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.access.rules import run_access_rules
from audit_workbench.change.data import ChangeData, load_change_data
from audit_workbench.change.rules import run_change_rules
from audit_workbench.core.evidence import EvidenceError
from audit_workbench.core.findings import Finding
from audit_workbench.crosslink import run_crosslink_rules
from audit_workbench.journal.data import JournalData, load_journal_data
from audit_workbench.journal.rules import run_journal_rules
from audit_workbench.reporting import ModuleName, module_of

GROUND_TRUTH_FILE = "ground_truth.json"


class RunError(ValueError):
    """The run could not be made: a missing audit date or an unusable input file."""


@dataclass(frozen=True)
class Run:
    """The loaded data of the selected modules and the findings of their rules."""

    access: AccessData | None
    journal: JournalData | None
    change: ChangeData | None
    findings: list[Finding]


def read_ground_truth(directory: Path) -> dict[str, Any] | None:
    path = directory / GROUND_TRUTH_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_a(directory: Path, as_of: date | None, truth: dict[str, Any] | None) -> date:
    """`--as-of` wins; otherwise take the access-review date from ground_truth.json."""
    if as_of is not None:
        return as_of
    if truth is None or "as_of" not in truth:
        raise RunError(
            f"--as-of is required because {directory / GROUND_TRUTH_FILE} does not exist"
        )
    return date.fromisoformat(truth["as_of"])


def resolve_b(directory: Path, as_of: date | None, truth: dict[str, Any] | None) -> date:
    """`--as-of` wins; otherwise the journal date (`journal_as_of`) or the bank's `as_of`."""
    if as_of is not None:
        return as_of
    for key in ("journal_as_of", "as_of"):
        if truth is not None and key in truth:
            return date.fromisoformat(truth[key])
    raise RunError(
        f"--as-of is required for module B because {directory / GROUND_TRUTH_FILE} "
        "has no journal_as_of"
    )


def resolve_c(directory: Path, as_of: date | None, truth: dict[str, Any] | None) -> date:
    """`--as-of` wins; otherwise the change date (`change_as_of`) or the bank's `as_of`."""
    if as_of is not None:
        return as_of
    for key in ("change_as_of", "as_of"):
        if truth is not None and key in truth:
            return date.fromisoformat(truth[key])
    raise RunError(
        f"--as-of is required for module C because {directory / GROUND_TRUTH_FILE} "
        "has no change_as_of"
    )


def load_run(directory: Path, as_of: date | None, module: ModuleName) -> Run:
    """Load the evidence the selected modules need and run their rules."""
    truth = read_ground_truth(directory)
    access: AccessData | None = None
    journal: JournalData | None = None
    change: ChangeData | None = None
    # Module X compares all three views, so it needs every data set even when only X is
    # asked for. Its rules read the A/B/C findings to link a cross-module case to the
    # single-module ones behind it, so those rules run as context and are filtered out
    # afterwards when only X was requested.
    needs_access = module in (ModuleName.a, ModuleName.all, ModuleName.x)
    needs_journal = module in (ModuleName.b, ModuleName.all, ModuleName.x)
    needs_change = module in (ModuleName.c, ModuleName.all, ModuleName.x)
    try:
        if needs_access:
            access = AccessData(
                evidence=load_access_evidence(directory), as_of=resolve_a(directory, as_of, truth)
            )
        if needs_journal:
            journal = load_journal_data(directory, as_of=resolve_b(directory, as_of, truth))
        if needs_change:
            change = load_change_data(directory, as_of=resolve_c(directory, as_of, truth))
        related: list[Finding] = []
        if access is not None:
            related += run_access_rules(access)
        if journal is not None:
            related += run_journal_rules(journal)
        if change is not None:
            related += run_change_rules(change)
        findings: list[Finding] = list(related)
        if module in (ModuleName.x, ModuleName.all) and access and journal and change:
            findings += run_crosslink_rules(access, journal, change, related)
        if module is ModuleName.x:
            findings = [f for f in findings if module_of(f.rule_id) == "X"]
    except EvidenceError as exc:
        raise RunError(str(exc)) from exc
    return Run(access, journal, change, findings)
