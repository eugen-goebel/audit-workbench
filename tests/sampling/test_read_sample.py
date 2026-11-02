"""`read_sample` reads back what `write_sample` wrote (used by `awb workpaper --sample`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_workbench.sampling import Sample, SampleItem, read_sample, write_sample

SHA = "a" * 64


def _sample() -> Sample:
    return Sample(
        population="changes",
        file="changes",
        sha256=SHA,
        population_size=1800,
        seed=3,
        replacements=2,
        parameters={
            "confidence": 0.95,
            "tolerable_rate": 0.05,
            "expected_rate": 0.01,
            "method": "binomial",
            "deviations_allowed": 1,
            "full_population": False,
        },
        items=(
            SampleItem(1, "CHG-00001"),
            SampleItem(2, "CHG-00002"),
            SampleItem(1, "CHG-00003", replacement=True),
            SampleItem(2, "CHG-00004", replacement=True),
        ),
    )


def test_a_written_sample_reads_back_equal(tmp_path: Path) -> None:
    sample = _sample()
    path = write_sample(sample, tmp_path / "s.csv")
    assert read_sample(path) == sample


def test_the_sample_without_replacements_reads_back(tmp_path: Path) -> None:
    sample = Sample(
        population="manual_entries",
        file="manual_entries",
        sha256=SHA,
        population_size=5,
        seed=1,
        replacements=0,
        parameters={"full_population": True},
        items=tuple(SampleItem(i, f"K{i}") for i in range(1, 6)),
    )
    assert read_sample(write_sample(sample, tmp_path / "s.csv")) == sample


@pytest.mark.parametrize("missing", ["population", "file_sha256", "size", "n", "seed"])
def test_a_missing_header_line_is_an_error(tmp_path: Path, missing: str) -> None:
    path = write_sample(_sample(), tmp_path / "s.csv")
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith(f"# {missing}:")]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match=missing):
        read_sample(path)


def test_a_file_without_the_item_table_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "s.csv"
    path.write_text("# population: changes\n")
    with pytest.raises(ValueError, match="rank,key,replacement"):
        read_sample(path)


def test_n_in_the_header_must_match_the_rows(tmp_path: Path) -> None:
    path = write_sample(_sample(), tmp_path / "s.csv")
    path.write_text(path.read_text().replace("# n: 2", "# n: 3"))
    with pytest.raises(ValueError, match="n"):
        read_sample(path)


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        read_sample(tmp_path / "nope.csv")
