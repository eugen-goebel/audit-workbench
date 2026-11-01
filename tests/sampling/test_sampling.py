"""Tests for the sampling module (phase 4 spec, section 4)."""

from __future__ import annotations

import csv
import json
from datetime import date
from math import ceil, log
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from audit_workbench.sampling import (
    POPULATIONS,
    Sample,
    SampleItem,
    SelectionDates,
    draw,
    evaluate,
    plan_sample,
    population_keys,
    sample_size,
    write_sample,
)
from audit_workbench.synth import generate_bank


def _dates(directory: Path) -> SelectionDates:
    truth = json.loads((directory / "ground_truth.json").read_text())
    return SelectionDates(
        as_of=date.fromisoformat(truth["as_of"]),
        period_start=date(2025, 10, 1),
        period_end=date(2026, 9, 30),
    )


@pytest.fixture(scope="module")
def bank(tmp_path_factory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("sampling"), seed=7)


# ---------------------------------------------------------------------------
# 4.1 size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confidence", "tolerable", "expected", "binomial", "hypergeometric"),
    [
        (0.95, 0.05, 0.00, 59, 40),
        (0.95, 0.05, 0.01, 93, 59),
        (0.95, 0.05, 0.02, 181, 73),
        (0.90, 0.05, 0.00, 45, 33),
        (0.95, 0.10, 0.00, 29, 25),
    ],
)
def test_reference_values_of_the_spec(
    confidence: float,
    tolerable: float,
    expected: float,
    binomial: int,
    hypergeometric: int,
) -> None:
    size = sample_size(confidence, tolerable, expected)
    assert size.n == binomial and size.method == "binomial"
    assert size.deviations_allowed == ceil(size.n * expected)
    hyper = sample_size(confidence, tolerable, expected, method="hypergeometric", population=90)
    assert hyper.n == hypergeometric and hyper.population == 90
    assert not hyper.full_population


def test_closed_form_for_zero_expected_rate() -> None:
    closed = log(0.05) / log(0.95)
    assert 58 < closed < 59
    assert sample_size(0.95, 0.05, 0.0).n == 59


def test_the_sample_size_is_the_smallest_working_one() -> None:
    size = sample_size(0.95, 0.05, 0.01)
    assert size.n == 93
    from scipy.stats import binom  # noqa: PLC0415

    for n in range(60, 93):
        assert binom.cdf(ceil(n * 0.01), n, 0.05) > 0.05
    assert binom.cdf(ceil(93 * 0.01), 93, 0.05) <= 0.05


def test_full_population_when_the_sample_reaches_the_population() -> None:
    size = sample_size(0.95, 0.05, 0.0, method="hypergeometric", population=10)
    assert size.n == 10 and size.full_population is True


@pytest.mark.parametrize(
    ("confidence", "tolerable", "expected"),
    [
        (0.0, 0.05, 0.0),
        (1.0, 0.05, 0.0),
        (0.95, 0.0, 0.0),
        (0.95, 1.0, 0.0),
        (0.95, 0.05, 0.05),
        (0.95, 0.05, -0.01),
    ],
)
def test_invalid_rates_raise(confidence: float, tolerable: float, expected: float) -> None:
    with pytest.raises(ValueError):
        sample_size(confidence, tolerable, expected)


def test_unknown_method_and_missing_population_raise() -> None:
    with pytest.raises(ValueError, match="method"):
        sample_size(0.95, 0.05, 0.0, method="poisson")
    with pytest.raises(ValueError, match="population"):
        sample_size(0.95, 0.05, 0.0, method="hypergeometric")


# ---------------------------------------------------------------------------
# 4.2 evaluation
# ---------------------------------------------------------------------------


def test_evaluate_uses_the_clopper_pearson_limit() -> None:
    result = evaluate(59, 0, 0.95, 0.05)
    assert result.upper_limit == pytest.approx(1 - 0.05 ** (1 / 59), rel=1e-9)
    assert result.within_tolerable is True


def test_evaluate_flags_too_many_deviations() -> None:
    # with 59 items and a 5 % tolerable rate not even one deviation fits
    for deviations in (1, 2, 4):
        assert evaluate(59, deviations, 0.95, 0.05).within_tolerable is False
    assert evaluate(300, 2, 0.95, 0.05).within_tolerable is True


def test_evaluate_edge_cases() -> None:
    assert evaluate(30, 30, 0.95, 0.05).upper_limit == 1.0
    assert evaluate(1, 0, 0.95, 0.05).upper_limit == pytest.approx(0.95, rel=1e-9)
    with pytest.raises(ValueError):
        evaluate(0, 0, 0.95, 0.05)
    with pytest.raises(ValueError):
        evaluate(10, 11, 0.95, 0.05)


# ---------------------------------------------------------------------------
# 4.3 selection
# ---------------------------------------------------------------------------


def test_every_population_is_non_empty_on_a_generated_bank(bank: Path) -> None:
    dates = _dates(bank)
    for name in POPULATIONS:
        evidence, keys = population_keys(bank, name, dates=dates)
        assert keys, name
        assert keys == sorted(set(keys)), name
        assert len(evidence.sha256) == 64


def test_manual_entries_are_only_manual(bank: Path) -> None:
    _, keys = population_keys(bank, "manual_entries", dates=_dates(bank))
    rows = list(csv.DictReader((bank / "journal_entries.csv").open()))
    manual = {r["doc_no"] for r in rows if r["source"] == "manual"}
    assert set(keys) == manual


def test_changes_stay_inside_the_period(bank: Path) -> None:
    dates = _dates(bank)
    _, keys = population_keys(bank, "changes", dates=dates)
    rows = {r["change_id"]: r["created_ts"] for r in csv.DictReader((bank / "changes.csv").open())}
    assert set(keys) == {
        cid
        for cid, ts in rows.items()
        if dates.period_start <= date.fromisoformat(ts[:10]) <= dates.period_end
    }


def test_prod_deployments_are_only_production(bank: Path) -> None:
    _, keys = population_keys(bank, "prod_deployments", dates=_dates(bank))
    rows = list(csv.DictReader((bank / "deployments.csv").open()))
    assert set(keys) == {r["deployment_id"] for r in rows if r["environment"] == "prod"}


def test_active_personal_accounts_have_no_technical_account(bank: Path) -> None:
    _, keys = population_keys(bank, "active_personal_accounts", dates=_dates(bank))
    rows = {r["account_id"]: r for r in csv.DictReader((bank / "accounts.csv").open())}
    assert set(keys) == {
        a for a, r in rows.items() if r["account_type"] == "personal" and r["status"] == "active"
    }


def test_unknown_population_raises(bank: Path) -> None:
    with pytest.raises(KeyError, match="unknown population"):
        population_keys(bank, "does_not_exist", dates=_dates(bank))


def test_same_seed_same_sample(bank: Path) -> None:
    dates = _dates(bank)
    first = draw(bank, "manual_entries", seed=7, n=40, dates=dates)
    second = draw(bank, "manual_entries", seed=7, n=40, dates=dates)
    third = draw(bank, "manual_entries", seed=8, n=40, dates=dates)
    assert first == second
    assert first.items != third.items
    assert len(first.sample) == 40 and len(first.replacement_items) == 5
    keys = [i.key for i in first.sample]
    assert len(set(keys)) == 40
    assert not {i.key for i in first.replacement_items} & set(keys)


def test_replacements_are_capped_by_the_population(bank: Path) -> None:
    dates = _dates(bank)
    _, keys = population_keys(bank, "recert_decisions", dates=dates)
    size = len(keys)
    sample = draw(bank, "recert_decisions", seed=11, n=size - 2, dates=dates, replacements=5)
    assert len(sample.sample) == size - 2
    assert len(sample.replacement_items) == 2


def test_too_many_items_raise(bank: Path) -> None:
    dates = _dates(bank)
    _, keys = population_keys(bank, "recert_decisions", dates=dates)
    with pytest.raises(ValueError, match="exceeds the population"):
        draw(bank, "recert_decisions", seed=7, n=len(keys) + 1, dates=dates)


def test_write_sample_records_the_header_and_the_rows(bank: Path, tmp_path: Path) -> None:
    dates = _dates(bank)
    sample = plan_sample(
        bank,
        "manual_entries",
        confidence=0.95,
        tolerable_rate=0.05,
        expected_rate=0.01,
        seed=7,
        dates=dates,
    )
    path = write_sample(sample, tmp_path / "out" / "sample.csv")
    lines = path.read_text().splitlines()
    header = [line for line in lines if line.startswith("# ")]
    assert any(line.startswith("# population: manual_entries") for line in header)
    assert any(
        line.startswith("# file_sha256: ") and len(line.split(": ")[1]) == 64 for line in header
    )
    assert any(line.startswith("# size: ") for line in header)
    assert any(line.startswith("# seed: 7") for line in header)
    assert any(line.startswith("# confidence: 0.95") for line in header)
    assert any(line.startswith("# deviations_allowed: ") for line in header)
    body = [line for line in lines if not line.startswith("# ")]
    assert body[0] == "rank,key,replacement"
    rows = list(csv.DictReader(body))
    assert len(rows) == sample.n + sample.replacements == 93 + 5
    assert sample.population_size > 93
    assert [r["key"] for r in rows if r["replacement"] == "false"] == [i.key for i in sample.sample]
    assert {r["replacement"] for r in rows} == {"true", "false"}


def test_plan_sample_uses_the_population_size_for_hypergeometric(bank: Path) -> None:
    dates = _dates(bank)
    sample = plan_sample(
        bank,
        "manual_entries",
        confidence=0.95,
        tolerable_rate=0.05,
        expected_rate=0.0,
        method="hypergeometric",
        seed=3,
        dates=dates,
    )
    assert sample.parameters["method"] == "hypergeometric"
    assert sample.parameters["full_population"] is False
    assert 0 < len(sample.sample) <= sample.population_size


@settings(max_examples=25, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=10_000),
    n=st.integers(min_value=1, max_value=20),
    replacements=st.integers(min_value=0, max_value=8),
)
def test_property_same_parameters_same_sample(
    bank: Path, seed: int, n: int, replacements: int
) -> None:
    """Same file hash, same parameters, same seed: same sample (spec 4.3)."""
    directory = bank
    dates = _dates(directory)
    _, keys = population_keys(directory, "changes", dates=dates)
    if n > len(keys):
        pytest.skip("population too small")
    first = draw(directory, "changes", seed=seed, n=n, dates=dates, replacements=replacements)
    second = draw(directory, "changes", seed=seed, n=n, dates=dates, replacements=replacements)
    assert first == second
    assert len(first.sample) == n
    assert {i.key for i in first.items} <= set(keys)
    assert len(first.replacement_items) == min(replacements, len(keys) - n)


def test_sample_dataclass_helpers() -> None:
    sample = Sample(
        population="p",
        file="f.csv",
        sha256="0" * 64,
        population_size=3,
        seed=1,
        replacements=1,
        items=(
            SampleItem(1, "a"),
            SampleItem(2, "b"),
            SampleItem(1, "c", replacement=True),
        ),
    )
    assert [i.key for i in sample.sample] == ["a", "b"]
    assert [i.key for i in sample.replacement_items] == ["c"]
    assert sample.as_dict()["items"][2] == {"rank": 1, "key": "c", "replacement": True}
