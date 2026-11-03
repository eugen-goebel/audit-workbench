"""One seed 7 bank and one full run, built once per test session (about 3.5 s)."""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_workbench.reporting import ModuleName
from audit_workbench.runner import Run, load_run, read_ground_truth
from audit_workbench.synth import generate_bank


@pytest.fixture(scope="session")
def bank7(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("dashboard") / "bank", seed=7)


@pytest.fixture(scope="session")
def run7(bank7: Path) -> Run:
    return load_run(bank7, None, ModuleName.all)


@pytest.fixture(scope="session")
def truth7(bank7: Path) -> dict:
    truth = read_ground_truth(bank7)
    assert truth is not None
    return truth
