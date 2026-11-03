"""Smoke tests of the Streamlit app with `streamlit.testing.v1.AppTest` (no browser, no server)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

from streamlit.testing.v1 import AppTest  # noqa: E402

pytestmark = pytest.mark.dashboard_app

APP = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"
PAGES = ["Overview", "Module drill-down", "Role graph", "Benford", "Cross-module"]
ENV = (
    "AWB_DASHBOARD_DATA_DIR",
    "AWB_DASHBOARD_SEED",
    "AWB_DASHBOARD_AS_OF",
    "AWB_DASHBOARD_MODULE",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def _app(page: str | None = None) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    if page is not None:
        at.sidebar.radio(key="page").set_value(page).run()
    return at


def _texts(at: AppTest) -> str:
    parts = (
        [m.value for m in at.markdown] + [m.value for m in at.info] + [m.value for m in at.caption]
    )
    return "\n".join(parts)


def _metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def test_app_starts_with_banner_and_131_findings() -> None:
    at = _app()
    assert not at.exception
    assert any("Synthetic data, seed 7" in i.value for i in at.info)
    assert any("audit_workbench.synth" in i.value for i in at.info)
    metrics = _metrics(at)
    assert metrics["Findings"] == "131"
    assert metrics["CRITICAL"] == "7"
    assert metrics["Rules fired / ran"] == "27 / 27"
    assert metrics["Injected issues matched"] == "131 / 131"
    assert metrics["Findings without a ground-truth counterpart"] == "0"


@pytest.mark.parametrize("page", PAGES)
def test_each_page_renders(page: str) -> None:
    at = _app(page)
    assert not at.exception, [e.value for e in at.exception]
    assert [h.value for h in at.header] == [page]


def test_drilldown_filters_and_opens_a_finding() -> None:
    at = _app("Module drill-down")
    at.radio(key="drill_module").set_value("B").run()
    assert not at.exception
    assert any(s.label == "Open a finding" for s in at.selectbox)
    at.multiselect(key="drill_rules_B").set_value(["B-04"]).run()
    assert not at.exception
    assert "6 findings" in _texts(at)
    assert any(h.value.startswith("B-04") for h in at.subheader)


def test_drilldown_shows_the_regulation_quote_and_raw_rows() -> None:
    at = _app("Module drill-down")
    at.radio(key="drill_module").set_value("A").run()
    at.multiselect(key="drill_rules_A").set_value(["A-01"]).run()
    assert any(m.value.startswith("> ") for m in at.markdown)
    assert any("Raw rows of accounts.csv" in e.label for e in at.expander)


def test_role_graph_highlights_a_conflict_and_looks_up_a_role() -> None:
    at = _app("Role graph")
    conflict = at.selectbox(key="graph_pick").options[1]
    at.selectbox(key="graph_pick").set_value(conflict).run()
    assert not at.exception
    assert any("The two paths" in s.value for s in at.subheader)
    at.selectbox(key="graph_role").set_value("R-GL-CLERK").run()
    assert not at.exception
    assert any(s.value.startswith("R-GL-CLERK") for s in at.subheader)


def test_benford_marks_small_groups_as_not_testable() -> None:
    at = _app("Benford")
    assert not at.exception
    assert "not testable" in _texts(at)
    at.radio(key="benford_by").set_value("account").run()
    assert not at.exception
    assert "72 groups" in _texts(at)


def test_cross_module_lists_the_eight_chains() -> None:
    at = _app("Cross-module")
    assert len(at.expander) == 8


def test_a_navigation_button_opens_the_matching_drilldown() -> None:
    at = _app("Cross-module")
    button = next(b for b in at.button if b.label.startswith("Open the X-01"))
    button.click().run()
    assert not at.exception
    assert at.sidebar.radio(key="page").value == "Module drill-down"
    assert at.radio(key="drill_module").value == "X"
    assert any(h.value.startswith("X-01") for h in at.subheader)


def test_seed_from_the_url_generates_that_bank() -> None:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.query_params["seed"] = "11"
    at.run()
    assert not at.exception
    assert any("seed 11" in i.value for i in at.info)
    assert (
        _metrics(at)["Injected issues matched"].split(" / ")[0]
        == _metrics(at)["Injected issues matched"].split(" / ")[1]
    )


@pytest.mark.parametrize("seed", ["abc", "-3", "1.5", "99999999999"])
def test_an_invalid_seed_is_rejected(seed: str) -> None:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.query_params["seed"] = seed
    at.run()
    assert not at.exception
    assert any("invalid seed" in e.value.lower() for e in at.error)
    assert not at.metric


def test_the_app_reads_a_data_folder(bank7: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWB_DASHBOARD_DATA_DIR", str(bank7))
    at = _app()
    assert not at.exception
    assert any(str(bank7) in i.value and "seed 7" in i.value for i in at.info)
    assert _metrics(at)["Findings"] == "131"


def test_a_single_module_run(bank7: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWB_DASHBOARD_DATA_DIR", str(bank7))
    monkeypatch.setenv("AWB_DASHBOARD_MODULE", "c")
    at = _app()
    assert not at.exception
    assert _metrics(at)["Findings"] == "26"
    assert _metrics(at)["Rules fired / ran"] == "6 / 6"
    assert _metrics(at)["Injected issues matched"] == "26 / 26"
    for page in PAGES:
        at.sidebar.radio(key="page").set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])


def test_a_broken_data_folder_shows_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWB_DASHBOARD_DATA_DIR", str(tmp_path))
    at = _app()
    assert not at.exception
    assert at.error and not at.metric
