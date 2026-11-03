"""`awb dashboard`: argument handling and the Streamlit call, with the subprocess mocked."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from audit_workbench import cli
from audit_workbench.cli import app

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Recorder:
    def __init__(self, returncode: int = 0, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[None]:
        self.calls.append((cmd, kwargs["env"]))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode)


@pytest.fixture
def streamlit(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", recorder)
    return recorder


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def test_defaults_generate_seed_7_and_start_streamlit_on_localhost(streamlit: Recorder) -> None:
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 0, result.output
    ((cmd, env),) = streamlit.calls
    assert cmd[1:4] == ["-m", "streamlit", "run"]
    assert Path(cmd[4]) == cli.DASHBOARD_APP and cli.DASHBOARD_APP.is_file()
    assert cli.DASHBOARD_APP.parts[-2:] == ("dashboard", "app.py")
    assert _flag(cmd, "--server.port") == "8501"
    assert _flag(cmd, "--server.address") == "127.0.0.1"
    assert _flag(cmd, "--server.headless") == "false"
    assert _flag(cmd, "--browser.gatherUsageStats") == "false"
    assert env["AWB_DASHBOARD_SEED"] == "7"
    assert "AWB_DASHBOARD_DATA_DIR" not in env or env["AWB_DASHBOARD_DATA_DIR"] == ""
    assert "http://127.0.0.1:8501" in result.output


def test_options_reach_streamlit_and_the_environment(streamlit: Recorder, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "dashboard",
            str(tmp_path),
            "--port",
            "9000",
            "--host",
            "0.0.0.0",
            "--no-browser",
            "--as-of",
            "2026-09-30",
            "--module",
            "b",
        ],
    )
    assert result.exit_code == 0, result.output
    ((cmd, env),) = streamlit.calls
    assert _flag(cmd, "--server.port") == "9000"
    assert _flag(cmd, "--server.address") == "0.0.0.0"
    assert _flag(cmd, "--server.headless") == "true"
    assert env["AWB_DASHBOARD_DATA_DIR"] == str(tmp_path)
    assert env["AWB_DASHBOARD_AS_OF"] == "2026-09-30"
    assert env["AWB_DASHBOARD_MODULE"] == "b"
    assert "http://0.0.0.0:9000" in result.output


def test_seed_goes_to_the_environment(streamlit: Recorder) -> None:
    result = runner.invoke(app, ["dashboard", "--seed", "11", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert streamlit.calls[0][1]["AWB_DASHBOARD_SEED"] == "11"


def test_seed_with_a_data_folder_is_a_usage_error(streamlit: Recorder, tmp_path: Path) -> None:
    result = runner.invoke(app, ["dashboard", str(tmp_path), "--seed", "3"])
    assert result.exit_code == 2
    assert "--seed" in result.output and not streamlit.calls


def test_invalid_seed_is_a_usage_error(streamlit: Recorder) -> None:
    result = runner.invoke(app, ["dashboard", "--seed", "-5"])
    assert result.exit_code == 2 and not streamlit.calls
    result = runner.invoke(app, ["dashboard", "--seed", "99999999999"])
    assert result.exit_code == 2 and "seed" in result.output and not streamlit.calls


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_invalid_port_is_rejected(streamlit: Recorder, port: str) -> None:
    result = runner.invoke(app, ["dashboard", "--port", port])
    assert result.exit_code == 2 and not streamlit.calls


def test_missing_folder_is_rejected(streamlit: Recorder, tmp_path: Path) -> None:
    result = runner.invoke(app, ["dashboard", str(tmp_path / "nope")])
    assert result.exit_code == 2 and not streamlit.calls


def test_unknown_module_is_rejected(streamlit: Recorder) -> None:
    assert runner.invoke(app, ["dashboard", "--module", "q"]).exit_code == 2


def test_missing_extra_names_the_install_command(
    streamlit: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_missing_dashboard_packages", lambda: ["streamlit", "plotly"])
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 2 and not streamlit.calls
    assert "uv sync --extra dashboard" in result.output and "streamlit" in result.output


def test_missing_app_file_is_reported(
    streamlit: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "DASHBOARD_APP", tmp_path / "dashboard" / "app.py")
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 2 and not streamlit.calls
    assert "app.py" in result.output


def test_streamlit_exit_code_is_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.subprocess, "run", Recorder(returncode=3))
    assert runner.invoke(app, ["dashboard"]).exit_code == 3


def test_ctrl_c_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.subprocess, "run", Recorder(raises=KeyboardInterrupt()))
    assert runner.invoke(app, ["dashboard"]).exit_code == 0


def test_help_lists_the_options() -> None:
    out = ANSI.sub("", runner.invoke(app, ["dashboard", "--help"]).output)
    for text in ("--seed", "--port", "--host", "--no-browser", "--as-of", "--module"):
        assert text in out
    assert "dashboard" in ANSI.sub("", runner.invoke(app, ["--help"]).output)
