"""Tests for the Benford statistics (first digit, first two digits)."""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest

from audit_workbench.journal.benford import (
    MIN_AMOUNT_CENTS,
    benford_groups,
    conformity,
    expected_proportions,
    first_digit_test,
    first_two_digits_test,
    leading_digits,
    result_from_counts,
    to_cents,
)
from tests.journal.builders import MiniLedger

# 20 amounts with first-digit counts 1:6 2:4 3:3 4:2 5:1 6:1 7:1 8:1 9:1.
AMOUNTS = [
    "1000.00", "10.00", "1999.99", "15.50", "12345.67", "100.00",  # six with digit 1
    "20.00", "2500.00", "299.99", "23.45",  # four with digit 2
    "30.10", "3999.99", "350.00",  # three with digit 3
    "40.00", "499.99",  # two with digit 4
    "5000.00", "600.00", "70.70", "8000.00", "99.99",  # one each of 5, 6, 7, 8, 9
]  # fmt: skip
COUNTS = [6, 4, 3, 2, 1, 1, 1, 1, 1]
# Benford: P(d) = log10(1 + 1/d)
EXPECTED = [
    0.301029995664,  # log10(2)
    0.176091259056,  # log10(3/2)
    0.124938736608,  # log10(4/3)
    0.096910013008,  # log10(5/4)
    0.079181246048,  # log10(6/5)
    0.066946789631,  # log10(7/6)
    0.057991946978,  # log10(8/7)
    0.051152522447,  # log10(9/8)
    0.045757490561,  # log10(10/9)
]


def test_first_digit_hand_computed_example():
    r = first_digit_test(to_cents(AMOUNTS))

    assert r.n == 20
    assert list(r.digits) == list(range(1, 10))
    assert list(r.observed_counts) == COUNTS
    np.testing.assert_allclose(r.expected, EXPECTED, atol=1e-9)
    np.testing.assert_allclose(r.observed, [c / 20 for c in COUNTS], atol=1e-12)

    # |observed - expected| per digit, by hand (observed = count / 20):
    #   d=1 |0.30 - 0.301029995664| = 0.001029995664
    #   d=2 |0.20 - 0.176091259056| = 0.023908740944
    #   d=3 |0.15 - 0.124938736608| = 0.025061263392
    #   d=4 |0.10 - 0.096910013008| = 0.003089986992
    #   d=5 |0.05 - 0.079181246048| = 0.029181246048
    #   d=6 |0.05 - 0.066946789631| = 0.016946789631
    #   d=7 |0.05 - 0.057991946978| = 0.007991946978
    #   d=8 |0.05 - 0.051152522447| = 0.001152522447
    #   d=9 |0.05 - 0.045757490561| = 0.004242509439
    # sum = 0.112605001532, MAD = sum / 9 = 0.012511666837
    assert r.mad == pytest.approx(0.012511666837, abs=1e-9)

    # chi-square = sum (O - n*P)^2 / (n*P), n = 20, by hand 0.498804228
    assert r.chi_square == pytest.approx(0.498804228, abs=1e-9)

    # z with continuity correction (Nigrini), 1/(2n) = 0.025:
    #   d=1: diff 0.001030 < 0.025, so no correction:
    #        0.001029995664 / sqrt(0.301029995664 * 0.698970004336 / 20) = 0.010041908
    #   d=5: (0.029181246048 - 0.025) / sqrt(0.079181246048 * 0.920818753952 / 20)
    #        = 0.004181246048 / 0.060379... = 0.069250431
    assert r.z[0] == pytest.approx(0.010041908, abs=1e-9)
    assert r.z[4] == pytest.approx(0.069250431, abs=1e-9)
    # |0.0042 - 0.025| -> d=9: (0.025 > diff) so no correction either
    assert r.z[8] == pytest.approx(0.0907982203, abs=1e-9)


def test_hand_example_label_is_marginal():
    # MAD 0.0125 is above the acceptable limit 0.012 and below 0.015.
    r = first_digit_test(to_cents(AMOUNTS))
    assert r.conformity == "marginally acceptable conformity"


def test_first_two_digits_on_the_same_amounts():
    r = first_two_digits_test(to_cents(AMOUNTS))
    assert r.n == 20
    assert list(r.digits) == list(range(10, 100))
    assert r.observed_counts[0] == 3  # 10: 1000.00, 10.00, 100.00
    assert r.observed_counts[89] == 1  # 99: 99.99
    assert r.observed_counts.sum() == 20
    assert r.expected[0] == pytest.approx(math.log10(1 + 1 / 10), abs=1e-12)  # 0.041392685158
    assert r.expected.sum() == pytest.approx(1.0, abs=1e-12)
    # MAD = mean over the 90 two-digit classes
    assert r.mad == pytest.approx(np.abs(r.observed - r.expected).mean(), abs=1e-15)


@pytest.mark.parametrize(
    ("amount", "first", "first_two"),
    [
        ("1000.00", 1, 10),
        ("10.00", 1, 10),
        ("100.00", 1, 10),
        ("99.99", 9, 99),
        ("999999.99", 9, 99),
        ("1000000.00", 1, 10),
        ("19.99", 1, 19),
        ("20.00", 2, 20),
        ("1234567.89", 1, 12),
        ("10000000.00", 1, 10),
        ("99999999999.99", 9, 99),
        ("0.99", 9, 99),  # leading digits of the cents integer, 0.99 is excluded elsewhere
    ],
)
def test_digit_extraction_is_exact(amount, first, first_two):
    cents = to_cents([amount])
    assert leading_digits(cents, 1)[0] == first
    assert leading_digits(cents, 2)[0] == first_two


def test_digit_extraction_has_no_float_log_error():
    # float log10 gives 15.0 (or 14.999...) at the edges of this range, which is where
    # a log-based digit count goes wrong
    cents = np.array([10**14, 10**14 - 1, 10**15, 10**15 - 1, 10**17, 10**17 - 1], dtype=np.int64)
    assert list(leading_digits(cents, 1)) == [1, 9, 1, 9, 1, 9]
    assert list(leading_digits(cents, 2)) == [10, 99, 10, 99, 10, 99]


def test_amounts_below_ten_are_excluded():
    assert MIN_AMOUNT_CENTS == 1000
    r = first_digit_test(to_cents(["9.99", "0.00", "10.00", "5.00", "25.00"]))
    assert r.n == 2
    assert list(r.observed_counts[:3]) == [1, 1, 0]


def test_no_usable_amounts_raises():
    with pytest.raises(ValueError, match="no amounts"):
        first_digit_test(to_cents(["9.99", "1.00"]))


def test_to_cents_is_exact_and_rejects_fractions_of_a_cent():
    assert list(to_cents(["0.10", Decimal("1234.5"), "7"])) == [10, 123450, 700]
    with pytest.raises(ValueError, match="cent"):
        to_cents(["1.005"])


def test_perfectly_benford_stratified_sample_has_tiny_mad():
    n = 9000
    u = (np.arange(n) + 0.5) / n
    cents = np.round(10 ** (1 + 6 * u) * 100).astype(np.int64)
    assert first_digit_test(cents).mad < 0.0005
    assert first_digit_test(cents).conformity == "close conformity"
    assert first_two_digits_test(cents).mad < 0.0005
    assert first_two_digits_test(cents).chi_square < 15


@pytest.mark.parametrize(
    ("mad", "digits", "label"),
    [
        (0.0, 1, "close conformity"),
        (0.006, 1, "close conformity"),
        (0.0061, 1, "acceptable conformity"),
        (0.012, 1, "acceptable conformity"),
        (0.0121, 1, "marginally acceptable conformity"),
        (0.015, 1, "marginally acceptable conformity"),
        (0.0151, 1, "nonconformity"),
        (0.0012, 2, "close conformity"),
        (0.0013, 2, "acceptable conformity"),
        (0.0018, 2, "acceptable conformity"),
        (0.0019, 2, "marginally acceptable conformity"),
        (0.0022, 2, "marginally acceptable conformity"),
        (0.0023, 2, "nonconformity"),
    ],
)
def test_conformity_uses_nigrini_thresholds(mad, digits, label):
    assert conformity(mad, digits) == label


def test_expected_proportions_sum_to_one():
    assert expected_proportions(1).sum() == pytest.approx(1.0, abs=1e-12)
    assert expected_proportions(2).shape == (90,)
    with pytest.raises(ValueError):
        expected_proportions(3)


def test_details_are_plain_python():
    d = first_digit_test(to_cents(AMOUNTS)).details()
    assert set(d) >= {"n", "mad", "chi_square", "conformity", "digits"}
    assert type(d["mad"]) is float
    assert d["digits"][0] == {
        "digit": 1,
        "expected": d["digits"][0]["expected"],
        "observed": 0.3,
        "count": 6,
        "z": d["digits"][0]["z"],
    }
    assert type(d["digits"][0]["count"]) is int


# --- simulation behind the min_n defaults (spec B-01, B-02) ----------------


def _mads(seed: int, n: int, test) -> np.ndarray:
    """MADs of 400 perfectly Benford samples of `n` amounts.

    Amounts are log-uniform over whole decades 10 ... 10,000,000, which is exactly
    Benford-distributed. Each size has its own generator so the sizes do not
    influence each other.
    """
    rng = np.random.default_rng([seed, n])
    samples = np.round(10 ** rng.uniform(1, 7, size=(400, n)) * 100).astype(np.int64)
    return np.array([test(row).mad for row in samples])


SEED = 0


def test_simulation_first_digit_min_n():
    # perfectly conforming samples of 300 amounts are often judged nonconforming ...
    assert (_mads(SEED, 300, first_digit_test) > 0.015).mean() > 0.2
    # ... at 1000 amounts none of them is (for this seed; in general about 0.1 % are)
    assert (_mads(SEED, 1000, first_digit_test) > 0.015).sum() == 0


def test_simulation_first_two_digits_min_n():
    assert (_mads(SEED, 1000, first_two_digits_test) > 0.0022).mean() > 0.5
    assert (_mads(SEED, 5000, first_two_digits_test) > 0.0022).sum() == 0


# --- grouping ---------------------------------------------------------------


def test_benford_groups_by_employee_and_account(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E2")
    ledger.account("GL-E2", "E2")
    ledger.entry(amount="100.00", preparer="GL-E1")
    ledger.entry(amount="200.00", preparer="GL-E1")
    ledger.entry(amount="3000.00", preparer="GL-E2", debit="4000")
    ledger.entry(amount="9.99", preparer="GL-E2")  # below 10.00, excluded
    ledger.entry(amount="500.00", source="automatic")  # not manual, not in B-01
    data = ledger.load(tmp_path)

    by_emp = benford_groups(data, "employee")
    assert sorted(by_emp) == ["E1", "E2"]
    assert by_emp["E1"].n == 4
    assert by_emp["E2"].n == 2
    assert list(by_emp["E1"].observed_counts[:3]) == [2, 2, 0]
    assert len(by_emp["E1"].digits) == 9  # first digit by default

    by_acc = benford_groups(data, "account")
    assert len(by_acc["6000"].digits) == 90  # first two digits by default
    assert by_acc["6000"].n == 3  # 100, 200 manual and 500 automatic; 9.99 is excluded
    assert by_acc["4000"].n == 1
    assert benford_groups(data, "account", digits=1)["4000"].digits[0] == 1

    with pytest.raises(ValueError):
        benford_groups(data, "month")  # type: ignore[arg-type]


def test_leading_digits_needs_enough_digits():
    with pytest.raises(ValueError, match="at least 2 digit"):
        leading_digits(np.array([5, 50]), 2)


def test_table_has_one_row_per_digit():
    table = first_two_digits_test(to_cents(AMOUNTS)).table()
    assert list(table.columns) == ["digit", "expected", "observed", "count", "z"]
    assert len(table) == 90
    assert len(first_digit_test(to_cents(AMOUNTS)).table()) == 9


def test_result_from_counts_rejects_an_empty_sample():
    with pytest.raises(ValueError, match="no amounts"):
        result_from_counts(np.zeros(9, dtype=np.int64), 1)
