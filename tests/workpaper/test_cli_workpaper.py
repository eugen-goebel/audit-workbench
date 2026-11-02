"""`awb workpaper` end to end (spec docs/design/phase-5-workpaper.md, sections 2, 5 and 7)."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from audit_workbench.cli import app

from .extract import docx_text, pdf_text, squash

runner = CliRunner()
EPOCH = {"SOURCE_DATE_EPOCH": "1793577600"}  # 2026-11-02 00:00:00 UTC


def _wp(bank: Path, *args: str, env: dict[str, str] | None = None):
    return runner.invoke(app, ["workpaper", str(bank), *args], env=env or EPOCH)


def test_default_output_is_workpaper_docx_in_the_data_folder(bank7: Path) -> None:
    target = bank7 / "workpaper.docx"
    target.unlink(missing_ok=True)
    result = _wp(bank7)
    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert "workpaper.docx" in result.stdout
    text = docx_text(target)
    assert "1. Cover" in text and "8. Sign-off" in text
    assert "2026-11-02" in text  # the run date came from SOURCE_DATE_EPOCH
    target.unlink()


def test_the_format_follows_the_suffix(bank7: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "wp.PDF"
    result = _wp(bank7, "--output", str(out))
    assert result.exit_code == 0, result.output
    assert out.read_bytes()[:5] == b"%PDF-"
    assert "8. Sign-off" in squash(pdf_text(out))


def test_any_other_suffix_is_a_usage_error_and_writes_nothing(bank7: Path, tmp_path: Path) -> None:
    out = tmp_path / "wp.txt"
    result = _wp(bank7, "--output", str(out))
    assert result.exit_code == 2
    assert "docx" in result.output and "pdf" in result.output
    assert not out.exists()


def test_options_reach_the_document(bank7: Path, tmp_path: Path, sample_file: Path) -> None:
    out = tmp_path / "wp.docx"
    result = _wp(
        bank7,
        "--output",
        str(out),
        "--module",
        "c",
        "--min-severity",
        "high",
        "--preparer",
        "Eugen G.",
        "--title",
        "Change review Q3",
        "--sample",
        str(sample_file),
    )
    assert result.exit_code == 0, result.output
    text = docx_text(out)
    assert "Change review Q3" in text
    assert "Eugen G." in text
    assert "6. Samples" in text
    assert "C-04" in text and "A-01" not in text.split("3. Procedures")[1].split("4. Results")[0]
    assert "are not listed in section 5" in text


def test_a_sample_of_other_data_fails_with_exit_1_and_writes_nothing(
    bank7: Path, tmp_path: Path, sample_file: Path
) -> None:
    other = tmp_path / "other.csv"
    lines = sample_file.read_text().splitlines()
    other.write_text(
        "\n".join(
            "# file_sha256: " + "0" * 64 if ln.startswith("# file_sha256:") else ln for ln in lines
        )
        + "\n"
    )
    out = tmp_path / "wp.docx"
    result = _wp(bank7, "--output", str(out), "--sample", str(other))
    assert result.exit_code == 1
    assert "different data" in result.output
    assert not out.exists()


def test_a_broken_sample_file_is_a_usage_error(bank7: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.csv"
    broken.write_text("not a sample\n")
    result = _wp(bank7, "--output", str(tmp_path / "wp.docx"), "--sample", str(broken))
    assert result.exit_code == 2
    assert "error:" in result.output


def test_a_sample_of_an_unknown_population_is_a_usage_error(
    bank7: Path, tmp_path: Path, sample_file: Path
) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(sample_file.read_text().replace("# population: changes", "# population: nope"))
    result = _wp(bank7, "--output", str(tmp_path / "wp.docx"), "--sample", str(bad))
    assert result.exit_code == 2
    assert "unknown population" in result.output


def test_without_an_audit_date_the_command_stops_with_exit_2(bank7: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(bank7, copy)
    (copy / "ground_truth.json").unlink()
    result = _wp(copy, "--output", str(tmp_path / "wp.docx"))
    assert result.exit_code == 2
    assert "--as-of is required" in result.output


def test_an_invalid_source_date_epoch_is_a_usage_error(bank7: Path, tmp_path: Path) -> None:
    result = _wp(bank7, "--output", str(tmp_path / "wp.docx"), env={"SOURCE_DATE_EPOCH": "x"})
    assert result.exit_code == 2
    assert "SOURCE_DATE_EPOCH" in result.output


@pytest.mark.parametrize("suffix", ["docx", "pdf"])
def test_same_input_twice_gives_the_same_text(bank7: Path, tmp_path: Path, suffix: str) -> None:
    first, second = tmp_path / f"a.{suffix}", tmp_path / f"b.{suffix}"
    for out in (first, second):
        result = _wp(bank7, "--output", str(out), "--preparer", "X")
        assert result.exit_code == 0, result.output
    read = docx_text if suffix == "docx" else pdf_text
    assert read(first) == read(second)


@pytest.mark.parametrize(("suffix", "blocked"), [("docx", "docx"), ("pdf", "reportlab")])
def test_without_the_extra_the_command_names_it(
    bank7: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, blocked: str
) -> None:
    monkeypatch.delitem(sys.modules, f"audit_workbench.workpaper.render_{suffix}", raising=False)
    # `import docx` (or reportlab) now raises ImportError, whatever is already imported
    for name in [m for m in sys.modules if m == blocked or m.startswith(f"{blocked}.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, blocked, None)
    out = tmp_path / f"wp.{suffix}"
    result = _wp(bank7, "--output", str(out))
    assert result.exit_code == 2
    assert "workpaper" in result.output
    assert "audit-workbench[workpaper]" in result.output
    assert "uv sync --extra workpaper" in result.output
    assert not out.exists()
    # the other commands keep working
    assert runner.invoke(app, ["sample-size"]).exit_code == 0


def test_the_help_lists_the_command_and_its_options() -> None:
    # On GitHub Actions rich colours the help even without a terminal, and the
    # escape codes split option names ("-" + code + "-output"); strip them.
    result = runner.invoke(app, ["workpaper", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for option in (
        "--output",
        "--as-of",
        "--module",
        "--min-severity",
        "--sample",
        "--preparer",
        "--title",
    ):
        assert option in plain
