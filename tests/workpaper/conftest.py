"""Shared fixtures: one injected bank (seed 7), its run, and the workpaper built from it.

Generating a bank and running all rules takes several seconds, so everything is built
once per test session.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from audit_workbench.reporting import ModuleName
from audit_workbench.runner import Run, load_run
from audit_workbench.sampling import SelectionDates, plan_sample, read_sample, write_sample
from audit_workbench.synth import generate_bank
from audit_workbench.workpaper.build import build_document
from audit_workbench.workpaper.model import Document

RUN_DATE = date(2026, 11, 2)


@pytest.fixture(scope="session")
def bank7(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("wp") / "bank7"
    generate_bank(out, seed=7, inject=True)
    return out


@pytest.fixture(scope="session")
def clean_bank(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("wp") / "clean"
    generate_bank(out, seed=7, inject=False)
    return out


@pytest.fixture(scope="session")
def run7(bank7: Path) -> Run:
    return load_run(bank7, None, ModuleName.all)


@pytest.fixture(scope="session")
def sample_file(bank7: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    dates = SelectionDates(
        as_of=date(2026, 9, 30), period_start=date(2025, 9, 30), period_end=date(2026, 9, 30)
    )
    drawn = plan_sample(
        bank7,
        "changes",
        confidence=0.95,
        tolerable_rate=0.05,
        expected_rate=0.01,
        method="binomial",
        seed=3,
        dates=dates,
        replacements=3,
    )
    return write_sample(drawn, tmp_path_factory.mktemp("sample") / "sample_changes.csv")


@pytest.fixture(scope="session")
def doc7(bank7: Path, run7: Run, sample_file: Path) -> Document:
    """The full workpaper of seed 7, with one sample."""
    return build_document(
        run7,
        directory=bank7,
        module=ModuleName.all,
        samples=[read_sample(sample_file)],
        preparer="A. Auditor",
        run_date=RUN_DATE,
    )
