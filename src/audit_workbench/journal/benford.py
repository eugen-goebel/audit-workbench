"""Benford's law tests on integer cents (Nigrini 2012).

An auditor uses these tests to see whether the amounts of a preparer or an
account follow the first-digit pattern that naturally grown amounts have. They
select where to look, they do not prove manipulation.

Digits are taken from the integer number of cents, never from a float
logarithm, so ``1000.00`` is always first digit 1 / first two digits 10 and
``99.99`` is 9 / 99. Amounts below 10.00 are excluded, so every tested amount
has at least two significant digits before the decimal point.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from audit_workbench.journal.data import JournalData

MIN_AMOUNT_CENTS = 1000
"""Amounts below 10.00 are not tested."""

# Nigrini's MAD limits: close / acceptable / marginally acceptable conformity.
# A MAD above the last limit is nonconformity.
THRESHOLDS: dict[int, tuple[float, float, float]] = {
    1: (0.006, 0.012, 0.015),
    2: (0.0012, 0.0018, 0.0022),
}
LABELS = (
    "close conformity",
    "acceptable conformity",
    "marginally acceptable conformity",
    "nonconformity",
)

_POW10 = 10 ** np.arange(19, dtype=np.int64)  # 1 ... 10**18, all exact in int64


def to_cents(values: Iterable[str | int | Decimal]) -> np.ndarray:
    """Convert decimal strings or `Decimal`s to exact integer cents.

    Raises `ValueError` for a value with a fraction of a cent.
    """
    out = []
    for value in values:
        scaled = (value if isinstance(value, Decimal) else Decimal(str(value))).scaleb(2)
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{value!r} has a fraction of a cent")
        out.append(int(scaled))
    return np.array(out, dtype=np.int64)


def leading_digits(cents: Iterable[int] | np.ndarray, k: int) -> np.ndarray:
    """The first `k` (1 or 2) significant digits of positive integers, exactly.

    The leading digits of the cents integer are the leading digits of the
    amount, because the decimal point only shifts by powers of ten.
    """
    c = np.asarray(cents, dtype=np.int64)
    if (c < _POW10[k - 1]).any():
        raise ValueError(f"need at least {k} digit(s) to extract {k} leading digit(s)")
    n_digits = np.searchsorted(_POW10, c, side="right")  # powers of ten <= c
    return c // _POW10[n_digits - k]


def expected_proportions(k: int) -> np.ndarray:
    """Benford proportions ``log10(1 + 1/d)`` for the first digit (k=1) or first two (k=2)."""
    if k not in THRESHOLDS:
        raise ValueError("k must be 1 (first digit) or 2 (first two digits)")
    d = np.arange(1, 10) if k == 1 else np.arange(10, 100)
    return np.log10(1 + 1 / d)


def conformity(mad: float, k: int) -> str:
    """Nigrini's verdict for a MAD value."""
    for limit, label in zip(THRESHOLDS[k], LABELS, strict=False):
        if mad <= limit:
            return label
    return LABELS[3]


@dataclass(frozen=True)
class BenfordResult:
    """Observed versus expected digit proportions of one group of amounts."""

    k: int
    n: int
    digits: np.ndarray
    expected: np.ndarray
    observed_counts: np.ndarray
    observed: np.ndarray
    mad: float
    chi_square: float
    z: np.ndarray
    conformity: str

    def table(self) -> pd.DataFrame:
        """One row per digit: expected, observed, count and z."""
        return pd.DataFrame(
            {
                "digit": self.digits,
                "expected": self.expected,
                "observed": self.observed,
                "count": self.observed_counts,
                "z": self.z,
            }
        )

    def details(self) -> dict[str, Any]:
        """JSON-friendly summary for a finding."""
        return {
            "n": self.n,
            "mad": float(self.mad),
            "chi_square": float(self.chi_square),
            "conformity": self.conformity,
            "nonconformity_threshold": THRESHOLDS[self.k][2],
            "digits": [
                {
                    "digit": int(d),
                    "expected": float(e),
                    "observed": float(o),
                    "count": int(c),
                    "z": float(z),
                }
                for d, e, o, c, z in zip(
                    self.digits,
                    self.expected,
                    self.observed,
                    self.observed_counts,
                    self.z,
                    strict=True,
                )
            ],
        }


def result_from_counts(counts: np.ndarray, k: int) -> BenfordResult:
    """Statistics from digit counts (90 or 9 classes)."""
    counts = np.asarray(counts, dtype=np.int64)
    n = int(counts.sum())
    if n == 0:
        raise ValueError("no amounts to test")
    expected = expected_proportions(k)
    observed = counts / n
    diff = np.abs(observed - expected)
    mad = float(diff.mean())
    chi_square = float(((counts - n * expected) ** 2 / (n * expected)).sum())
    # Nigrini's continuity correction 1/(2n) is applied only where it is smaller
    # than the difference itself.
    correction = 1 / (2 * n)
    adjusted = np.where(diff > correction, diff - correction, diff)
    z = adjusted / np.sqrt(expected * (1 - expected) / n)
    return BenfordResult(
        k=k,
        n=n,
        digits=np.arange(1, 10) if k == 1 else np.arange(10, 100),
        expected=expected,
        observed_counts=counts,
        observed=observed,
        mad=mad,
        chi_square=chi_square,
        z=z,
        conformity=conformity(mad, k),
    )


def _digit_classes(k: int) -> tuple[int, int]:
    return (1, 9) if k == 1 else (10, 90)


def _test(cents: Iterable[int] | np.ndarray, k: int) -> BenfordResult:
    c = np.asarray(cents, dtype=np.int64)
    c = c[c >= MIN_AMOUNT_CENTS]
    if c.size == 0:
        raise ValueError("no amounts of at least 10.00 to test")
    first, classes = _digit_classes(k)
    counts = np.bincount(leading_digits(c, k) - first, minlength=classes)
    return result_from_counts(counts, k)


def first_digit_test(cents: Iterable[int] | np.ndarray) -> BenfordResult:
    """First-digit test; amounts below 10.00 are excluded."""
    return _test(cents, 1)


def first_two_digits_test(cents: Iterable[int] | np.ndarray) -> BenfordResult:
    """First-two-digits test; amounts below 10.00 are excluded."""
    return _test(cents, 2)


def benford_groups(
    data: JournalData, by: Literal["employee", "account"], digits: int | None = None
) -> dict[str, BenfordResult]:
    """Digit tests for every group, including groups too small to judge.

    * ``by="employee"``: line amounts of manual entries per preparer employee
      (first digit by default; basis of B-01).
    * ``by="account"``: line amounts of all entries per GL account (first two
      digits by default; basis of B-02).

    Callers apply `min_n` themselves, so the CLI can list small groups too.
    """
    if by not in ("employee", "account"):
        raise ValueError("by must be 'employee' or 'account'")
    k = digits or (1 if by == "employee" else 2)
    pop = data.amount_lines
    if by == "employee":
        pop = pop[(pop["source"] == "manual") & pop["preparer_employee_id"].notna()]
        keys = pop["preparer_employee_id"]
    else:
        keys = pop["account_no"]
    keep = pop["amount_cents"].to_numpy() >= MIN_AMOUNT_CENTS
    if not keep.any():
        return {}
    cents = pop["amount_cents"].to_numpy()[keep]
    codes, uniques = pd.factorize(keys[keep], sort=True)
    first, classes = _digit_classes(k)
    flat = codes * classes + (leading_digits(cents, k) - first)
    table = np.bincount(flat, minlength=len(uniques) * classes).reshape(len(uniques), classes)
    return {str(key): result_from_counts(row, k) for key, row in zip(uniques, table, strict=True)}
