"""Combine the three mini builders into one bank for cross-module tests.

``MiniBank`` (access), ``MiniLedger`` (journal) and ``MiniChanges`` (change)
each write their own file set, and each also writes ``hr_employees.csv`` and
``accounts.csv`` because their loaders need them. ``World`` writes all three
sets into one directory and then replaces the two shared files with the merge
of all three builders (first definition of an id wins, access before ledger
before changes), so the three real loaders read one consistent bank.

    world = gl_world()
    world.ledger.entry(preparer="GL-E1", approver="GL-E1", ...)
    loaded = world.load(tmp_path)
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from audit_workbench.access.data import SCHEMA as ACCESS_SCHEMA
from audit_workbench.access.data import AccessData, load_access_evidence
from audit_workbench.change.data import ChangeData, load_change_data
from audit_workbench.journal.data import JournalData, load_journal_data
from tests.access.builders import MiniBank
from tests.change.builders import MiniChanges
from tests.journal.builders import MiniLedger

_SHARED = {"hr_employees": "employee_id", "accounts": "account_id"}


@dataclass(frozen=True)
class Loaded:
    access: AccessData
    journal: JournalData
    change: ChangeData


class World:
    def __init__(self) -> None:
        self.access = MiniBank()
        self.ledger = MiniLedger()
        self.changes = MiniChanges()

    def write(self, directory: Path) -> Path:
        directory = Path(directory)
        builders = (self.access, self.ledger, self.changes)  # first definition wins
        merged: dict[str, dict[str, dict[str, str]]] = {name: {} for name in _SHARED}
        for builder in builders:
            for name, key in _SHARED.items():
                for row in builder.rows[name]:
                    merged[name].setdefault(row[key], row)
        for builder in reversed(builders):
            builder.write(directory)
        for name in _SHARED:
            with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=ACCESS_SCHEMA[name], lineterminator="\n")
                writer.writeheader()
                writer.writerows(merged[name].values())
        return directory

    def load(self, directory: Path) -> Loaded:
        directory = self.write(directory)
        return Loaded(
            access=AccessData(evidence=load_access_evidence(directory), as_of=self.access.as_of),
            journal=load_journal_data(directory),
            change=load_change_data(directory),
        )


def gl_world() -> World:
    """GL roles and three clerks with GL accounts, no assignments yet.

    - ``R-POST`` grants ``GL_POST``, ``R-APPR`` grants ``GL_APPROVE``,
      ``R-VIEW`` grants ``GL_VIEW``; ``R-LEAD`` contains ``R-POST``.
    - Employees ``E1``, ``E2``, ``E3`` with accounts ``GL-E1``, ``GL-E2``,
      ``GL-E3``; ``GL-BATCH`` is the technical account of automatic entries.
    - ``LEAV`` left on 2025-11-15, with accounts ``GL-LEAV`` and ``AD-LEAV``.
    """
    world = World()
    bank = world.access
    for role, ent, function in (
        ("R-POST", "E-POST", "GL_POST"),
        ("R-APPR", "E-APPR", "GL_APPROVE"),
        ("R-VIEW", "E-VIEW", "GL_VIEW"),
    ):
        bank.role(role, system="GL").entitlement(ent, function, system="GL").grants(role, ent)
    bank.role("R-LEAD", system="GL").contains("R-LEAD", "R-POST")
    for emp in ("E1", "E2", "E3"):
        bank.employee(emp, org_unit="Rechnungswesen")
        bank.account(f"GL-{emp}", employee_id=emp, system="GL")
    bank.account("GL-BATCH", system="GL", account_type="technical", interactive=False)
    bank.employee("LEAV", org_unit="Rechnungswesen", exit_date="2025-11-15")
    bank.account("GL-LEAV", employee_id="LEAV", system="GL")
    bank.account("AD-LEAV", employee_id="LEAV", system="AD")
    return world
