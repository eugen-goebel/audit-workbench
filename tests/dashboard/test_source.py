"""Where the dashboard gets its run: a data folder or a generated bank."""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_workbench.dashboard import source
from audit_workbench.reporting import ModuleName
from audit_workbench.runner import Run


@pytest.mark.parametrize(("raw", "seed"), [("11", 11), (" 7 ", 7), ("0", 0), (5, 5)])
def test_valid_seeds(raw: str | int, seed: int) -> None:
    assert source.parse_seed(raw) == seed


def test_missing_seed_is_the_default() -> None:
    assert source.parse_seed(None) == source.DEFAULT_SEED == 7


@pytest.mark.parametrize("raw", ["abc", "", "1.5", "-1", "99999999999", "7; drop", True])
def test_invalid_seeds_are_rejected(raw: object) -> None:
    with pytest.raises(source.InvalidSeedError):
        source.parse_seed(raw)  # type: ignore[arg-type]


def test_folder_source_reads_the_folder(bank7: Path, run7: Run) -> None:
    loaded = source.load_source(bank7, None, ModuleName.all, source.DEFAULT_SEED)
    assert not loaded.generated and loaded.seed == 7 and loaded.directory == bank7
    assert {f.finding_id for f in loaded.run.findings} == {f.finding_id for f in run7.findings}
    assert loaded.truth is not None and loaded.truth["seed"] == 7


def test_generated_source_matches_the_folder_run_and_leaves_nothing_behind(run7: Run) -> None:
    loaded = source.load_source(None, None, ModuleName.all, 7)
    assert loaded.generated and loaded.seed == 7 and loaded.directory is None
    assert len(loaded.run.findings) == 131
    assert {f.finding_id for f in loaded.run.findings} == {f.finding_id for f in run7.findings}
    assert loaded.truth is not None and loaded.truth["seed"] == 7


def test_unusable_folder_raises_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(source.SourceError):
        source.load_source(tmp_path, None, ModuleName.all, 7)
