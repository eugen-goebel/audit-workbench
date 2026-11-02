"""Sampling for attribute tests (spec ``docs/design/phase-4-crosslink.md`` section 4).

Two jobs, both reproducible from the data and the seed alone:

* **Size** - how many items an attribute test needs so that a tolerable
  deviation rate can still be confirmed at a given confidence (4.1), and the
  evaluation of a finished sample with the one-sided Clopper-Pearson limit
  (4.2, ISA 530 para. 15).
* **Selection** - a random sample plus replacement items from one of the named
  populations, written to a file with a header that records the population, the
  file hash, the size and every parameter (4.3, ISA 530 paras. 7, 8, 10).

The numbers are statistical, the sample is a permutation: the same file hash,
the same parameters and the same seed give the same sample. Nothing here reads
the clock or the global ``random`` module.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from math import ceil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import beta, binom, hypergeom

from audit_workbench.core.evidence import Evidence, load_evidence

#: float slack when comparing a probability with ``1 - confidence``
_EPS = 1e-12
#: hard stop for the size search; binomial needs far less for realistic inputs
_MAX_N = 100_000

METHODS = ("binomial", "hypergeometric")


@dataclass(frozen=True)
class SampleSize:
    """Result of `sample_size`: the number of items and how it was derived."""

    n: int
    method: str
    confidence: float
    tolerable_rate: float
    expected_rate: float
    deviations_allowed: int
    population: int | None = None
    full_population: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "method": self.method,
            "confidence": self.confidence,
            "tolerable_rate": self.tolerable_rate,
            "expected_rate": self.expected_rate,
            "deviations_allowed": self.deviations_allowed,
            "population": self.population,
            "full_population": self.full_population,
        }


@dataclass(frozen=True)
class Evaluation:
    """Result of `evaluate`: the upper deviation limit and the conclusion."""

    upper_limit: float
    within_tolerable: bool
    n: int
    deviations: int
    confidence: float
    tolerable_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "upper_limit": self.upper_limit,
            "within_tolerable": self.within_tolerable,
            "n": self.n,
            "deviations": self.deviations,
            "confidence": self.confidence,
            "tolerable_rate": self.tolerable_rate,
        }


@dataclass(frozen=True)
class SampleItem:
    """One row of a sample file: its rank, the population key, and its role."""

    rank: int
    key: str
    replacement: bool = False


@dataclass(frozen=True)
class Sample:
    """A drawn sample plus the header that documents it."""

    population: str
    file: str
    sha256: str
    population_size: int
    seed: int
    replacements: int
    parameters: dict[str, Any] = field(default_factory=dict)
    items: tuple[SampleItem, ...] = ()

    @property
    def n(self) -> int:
        """Number of sampled items, without the replacement items."""
        return len(self.sample)

    @property
    def sample(self) -> tuple[SampleItem, ...]:
        return tuple(i for i in self.items if not i.replacement)

    @property
    def replacement_items(self) -> tuple[SampleItem, ...]:
        return tuple(i for i in self.items if i.replacement)

    def as_dict(self) -> dict[str, Any]:
        return {
            "population": self.population,
            "file": self.file,
            "sha256": self.sha256,
            "population_size": self.population_size,
            "n": self.n,
            "seed": self.seed,
            "replacements": self.replacements,
            "parameters": dict(self.parameters),
            "items": [
                {"rank": i.rank, "key": i.key, "replacement": i.replacement} for i in self.items
            ],
        }


# ---------------------------------------------------------------------------
# 4.1 size and 4.2 evaluation
# ---------------------------------------------------------------------------


def _check_rates(confidence: float, tolerable_rate: float, expected_rate: float) -> None:
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence!r}")
    if not 0 < tolerable_rate < 1:
        raise ValueError(f"tolerable_rate must be between 0 and 1, got {tolerable_rate!r}")
    if not 0 <= expected_rate < tolerable_rate:
        raise ValueError(
            f"expected_rate must satisfy 0 <= expected_rate < tolerable_rate, "
            f"got {expected_rate!r} and {tolerable_rate!r}"
        )


def _cdf(k: int, n: int, tolerable_rate: float, method: str, population: int | None) -> float:
    if method == "binomial":
        return float(binom.cdf(k, n, tolerable_rate))
    assert population is not None
    return float(hypergeom.cdf(k, population, ceil(tolerable_rate * population), n))


def sample_size(
    confidence: float,
    tolerable_rate: float,
    expected_rate: float,
    method: str = "binomial",
    population: int | None = None,
) -> SampleSize:
    """Smallest sample whose allowed deviations still confirm the tolerable rate.

    With ``k = ceil(n * expected_rate)`` the sample works when
    ``P(X <= k) <= 1 - confidence``. The hypergeometric variant needs the known
    population size. If the search reaches the population size, the whole
    population is tested and ``full_population`` is set.
    """
    _check_rates(confidence, tolerable_rate, expected_rate)
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, got {method!r}")
    if method == "hypergeometric":
        if population is None or population <= 0:
            raise ValueError("method 'hypergeometric' needs a positive population size")
    elif population is not None and population <= 0:
        raise ValueError(f"population must be positive, got {population!r}")

    limit = population if population is not None else _MAX_N
    for n in range(1, min(limit, _MAX_N) + 1):
        k = ceil(n * expected_rate)
        if _cdf(k, n, tolerable_rate, method, population) <= 1 - confidence + _EPS:
            return SampleSize(
                n=n,
                method=method,
                confidence=confidence,
                tolerable_rate=tolerable_rate,
                expected_rate=expected_rate,
                deviations_allowed=k,
                population=population,
                full_population=population is not None and n >= population,
            )
    if population is None:
        raise ValueError(
            f"no sample size up to {_MAX_N} confirms {tolerable_rate!r} at {confidence!r}"
        )
    return SampleSize(
        n=population,
        method=method,
        confidence=confidence,
        tolerable_rate=tolerable_rate,
        expected_rate=expected_rate,
        deviations_allowed=ceil(population * expected_rate),
        population=population,
        full_population=True,
    )


def evaluate(n: int, deviations: int, confidence: float, tolerable_rate: float) -> Evaluation:
    """One-sided upper deviation limit (Clopper-Pearson) and the conclusion."""
    _check_rates(confidence, tolerable_rate, 0.0)
    if n <= 0:
        raise ValueError(f"n must be positive, got {n!r}")
    if not 0 <= deviations <= n:
        raise ValueError(f"deviations must be between 0 and n, got {deviations!r} for n={n!r}")
    if deviations == n:
        upper = 1.0
    else:
        upper = float(beta.ppf(confidence, deviations + 1, n - deviations))
    return Evaluation(
        upper_limit=upper,
        within_tolerable=upper <= tolerable_rate + _EPS,
        n=n,
        deviations=deviations,
        confidence=confidence,
        tolerable_rate=tolerable_rate,
    )


# ---------------------------------------------------------------------------
# 4.3 selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Population:
    """One named population: an evidence file and the key of each item."""

    name: str
    file: str
    keys: Callable[[pd.DataFrame, SelectionDates], Iterable[str]]
    required: tuple[str, ...]


@dataclass(frozen=True)
class SelectionDates:
    """Reference dates a population filter needs."""

    as_of: date
    period_start: date
    period_end: date


def _prod_deployments(frame: pd.DataFrame, _: SelectionDates) -> Iterable[str]:
    return frame.loc[frame["environment"] == "prod", "deployment_id"]


def _changes(frame: pd.DataFrame, dates: SelectionDates) -> Iterable[str]:
    created = frame["created_ts"].str.slice(0, 10)
    keep = (created >= dates.period_start.isoformat()) & (created <= dates.period_end.isoformat())
    return frame.loc[keep, "change_id"]


def _manual_entries(frame: pd.DataFrame, _: SelectionDates) -> Iterable[str]:
    return frame.loc[frame["source"] == "manual", "doc_no"]


def _recert_decisions(frame: pd.DataFrame, _: SelectionDates) -> Iterable[str]:
    return (
        frame["campaign_id"].astype(str)
        + "|"
        + frame["account_id"].astype(str)
        + "|"
        + frame["role_id"].astype(str)
    )


def _active_personal_accounts(frame: pd.DataFrame, dates: SelectionDates) -> Iterable[str]:
    keep = (
        (frame["account_type"] == "personal")
        & (frame["status"] == "active")
        & (frame["created_at"].str.slice(0, 10) <= dates.as_of.isoformat())
    )
    return frame.loc[keep, "account_id"]


POPULATIONS: dict[str, Population] = {
    "prod_deployments": Population(
        "prod_deployments",
        "deployments.csv",
        _prod_deployments,
        ("deployment_id", "environment"),
    ),
    "changes": Population("changes", "changes.csv", _changes, ("change_id", "created_ts")),
    "manual_entries": Population(
        "manual_entries", "journal_entries.csv", _manual_entries, ("doc_no", "source")
    ),
    "recert_decisions": Population(
        "recert_decisions",
        "recert_decisions.csv",
        _recert_decisions,
        ("campaign_id", "account_id", "role_id"),
    ),
    "active_personal_accounts": Population(
        "active_personal_accounts",
        "accounts.csv",
        _active_personal_accounts,
        ("account_id", "account_type", "status", "created_at"),
    ),
}


def population_keys(
    directory: Path | str, name: str, *, dates: SelectionDates
) -> tuple[Evidence, list[str]]:
    """The sorted, unique keys of one named population and its evidence file."""
    try:
        population = POPULATIONS[name]
    except KeyError:
        raise KeyError(
            f"unknown population {name!r}, expected one of {sorted(POPULATIONS)}"
        ) from None
    evidence = load_evidence(
        Path(directory) / population.file, name=name, required_columns=population.required
    )
    keys = sorted({str(k) for k in population.keys(evidence.frame, dates)})
    return evidence, keys


def draw(
    directory: Path | str,
    name: str,
    *,
    seed: int,
    n: int,
    dates: SelectionDates,
    replacements: int = 5,
    parameters: dict[str, Any] | None = None,
) -> Sample:
    """Draw *n* items plus replacement items from one named population."""
    if n <= 0:
        raise ValueError(f"n must be positive, got {n!r}")
    if replacements < 0:
        raise ValueError(f"replacements must not be negative, got {replacements!r}")
    evidence, keys = population_keys(directory, name, dates=dates)
    population_size = len(keys)
    if population_size == 0:
        raise ValueError(f"population {name!r} is empty in {directory!r}")
    if n > population_size:
        raise ValueError(f"n={n} exceeds the population size {population_size} of {name!r}")
    spare = min(replacements, population_size - n)
    order = np.random.default_rng(seed).permutation(population_size)
    items = [
        *(SampleItem(rank=rank, key=keys[int(order[rank - 1])]) for rank in range(1, n + 1)),
        *(
            SampleItem(rank=rank, key=keys[int(order[n + rank - 1])], replacement=True)
            for rank in range(1, spare + 1)
        ),
    ]
    return Sample(
        population=name,
        file=evidence.name,
        sha256=evidence.sha256,
        population_size=population_size,
        seed=seed,
        replacements=spare,
        parameters=dict(parameters or {}),
        items=tuple(items),
    )


def plan_sample(
    directory: Path | str,
    name: str,
    *,
    confidence: float,
    tolerable_rate: float,
    expected_rate: float,
    method: str = "binomial",
    seed: int,
    dates: SelectionDates,
    replacements: int = 5,
) -> Sample:
    """`sample_size` from the population size, then `draw` with those parameters."""
    _, keys = population_keys(directory, name, dates=dates)
    population = len(keys)
    size = sample_size(
        confidence,
        tolerable_rate,
        expected_rate,
        method=method,
        population=population if method == "hypergeometric" else None,
    )
    parameters: dict[str, Any] = {
        "confidence": confidence,
        "tolerable_rate": tolerable_rate,
        "expected_rate": expected_rate,
        "method": method,
        "deviations_allowed": size.deviations_allowed,
        "full_population": size.full_population,
    }
    return draw(
        directory,
        name,
        seed=seed,
        n=size.n,
        dates=dates,
        replacements=replacements,
        parameters=parameters,
    )


def write_sample(sample: Sample, path: Path | str) -> Path:
    """Write the sample file: header lines, then ``rank,key,replacement``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        ("population", sample.population),
        ("file", sample.file),
        ("file_sha256", sample.sha256),
        ("size", sample.population_size),
        ("n", sample.n),
        ("seed", sample.seed),
        ("replacements", sample.replacements),
        *((k, v) for k, v in sorted(sample.parameters.items())),
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        for key, value in header:
            fh.write(f"# {key}: {value}\n")
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["rank", "key", "replacement"])
        for item in sample.items:
            writer.writerow([item.rank, item.key, "true" if item.replacement else "false"])
    return path
