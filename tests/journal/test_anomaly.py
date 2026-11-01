"""Tests for the anomaly ranking of manual entries (not a rule, no findings)."""

from __future__ import annotations

import pandas as pd

from audit_workbench.journal.anomaly import FEATURES, entry_features, rank_entries
from tests.journal.builders import MiniLedger


def _ledger(*, reverse: bool = False) -> tuple[MiniLedger, str]:
    """80 ordinary manual entries and one that is odd in every respect."""
    ledger = MiniLedger()
    ledger.employee("E2", org_unit="IT", job_function="Administration")
    ledger.account("GL-E2", "E2")
    rows = []
    for i in range(80):
        day = 3 + (i % 20)  # 3 ... 22 March 2025
        while pd.Timestamp(2025, 3, day).weekday() >= 5:
            day += 1
        rows.append(
            dict(
                doc_no=f"MJ-2025-{i + 1:06d}",
                amount=f"{500 + (i * 37) % 1500}.{(i * 13) % 100:02d}",
                posting_date=f"2025-03-{day:02d}",
                entry_ts=f"2025-03-{day:02d}T{9 + i % 8:02d}:{(i * 7) % 60:02d}:00",
            )
        )
    odd = dict(
        doc_no="MJ-2025-000081",
        amount="9500000.00",
        debit="3000",
        credit="9999",
        posting_date="2025-12-31",
        entry_ts="2025-12-28T03:12:00",  # a Sunday, at night
        preparer="GL-E2",
    )
    rows.append(odd)
    for row in reversed(rows) if reverse else rows:
        ledger.entry(**row)
    return ledger, "MJ-2025-000081"


def test_odd_entry_is_ranked_first(tmp_path):
    ledger, odd = _ledger()
    ranked = rank_entries(ledger.load(tmp_path))

    assert ranked.iloc[0]["doc_no"] == odd
    assert list(ranked["rank"]) == list(range(1, 82))
    assert ranked["score"].is_monotonic_decreasing
    assert ranked.iloc[0]["score"] > ranked.iloc[1]["score"]
    assert set(FEATURES) <= set(ranked.columns)
    assert {"doc_no", "score", "rank", "preparer_employee_id", "amount"} <= set(ranked.columns)
    assert ranked.iloc[0]["preparer_employee_id"] == "E2"


def test_top_limits_the_result(tmp_path):
    ledger, odd = _ledger()
    data = ledger.load(tmp_path)
    top = rank_entries(data, top=5)
    assert len(top) == 5
    assert top.iloc[0]["doc_no"] == odd
    assert list(top["doc_no"]) == list(rank_entries(data)["doc_no"][:5])
    assert len(rank_entries(data, top=0)) == 0
    assert len(rank_entries(data, top=1000)) == 81


def test_ranking_is_deterministic_and_independent_of_row_order(tmp_path):
    ledger, _ = _ledger()
    a = rank_entries(ledger.load(tmp_path / "a"))
    b = rank_entries(ledger.load(tmp_path / "b"))
    pd.testing.assert_frame_equal(a, b)

    reversed_ledger, _ = _ledger(reverse=True)
    c = rank_entries(reversed_ledger.load(tmp_path / "c"))
    merged = a.merge(c, on="doc_no", suffixes=("_a", "_c"))
    assert (merged["score_a"] - merged["score_c"]).abs().max() < 1e-12


def test_only_manual_entries_are_ranked(tmp_path):
    ledger, _ = _ledger()
    ledger.entry("AJ-2025-000001", source="automatic", amount="99999999.00")
    ranked = rank_entries(ledger.load(tmp_path))
    assert len(ranked) == 81
    assert not ranked["doc_no"].str.startswith("AJ").any()


def test_ranking_produces_no_findings(tmp_path):
    ledger, _ = _ledger()
    result = rank_entries(ledger.load(tmp_path))
    assert isinstance(result, pd.DataFrame)


def test_empty_and_tiny_ledgers(tmp_path):
    ledger = MiniLedger()
    ledger.entry("AJ-2025-000001", source="automatic")
    empty = rank_entries(ledger.load(tmp_path / "a"))
    assert empty.empty
    assert {"doc_no", "score", "rank", *FEATURES} <= set(empty.columns)

    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    one = rank_entries(ledger.load(tmp_path / "b"))
    assert list(one["doc_no"]) == ["MJ-2025-000001"]
    assert list(one["rank"]) == [1]


def test_features_follow_the_spec(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E2")
    ledger.account("GL-E2", "E2")
    ledger.entry(
        "MJ-2025-000001",
        amount="1234.50",
        posting_date="2025-03-10",
        entry_ts="2025-03-08T10:30:00",  # Saturday
        debit="1200",
        credit="4000",
    )
    ledger.entry("MJ-2025-000002", amount="10.00", debit="1200", credit="4000", preparer="GL-E2")
    ledger.entry("MJ-2025-000003", amount="10.00", debit="6000", credit="1600")
    ledger.entry("MJ-2025-000004", amount="10.00", debit="6000", credit="1600")
    ledger.entry("AJ-2025-000001", source="automatic", debit="1200", credit="4000")
    feats = entry_features(ledger.load(tmp_path)).set_index("doc_no")

    row = feats.loc["MJ-2025-000001"]
    assert row["amount"] == 123450 / 100  # float inside statistics only
    assert row["hour"] == 10.5
    assert row["weekday"] == 5  # Saturday
    assert row["days_to_period_end"] == 21  # 10 March -> 31 March
    assert row["pair_frequency"] == 3  # (1200, 4000): two manual entries and one automatic
    assert row["preparer_volume"] == 3  # E1 keyed 000001, 000003 and 000004
    assert feats.loc["MJ-2025-000003", "pair_frequency"] == 2
    assert feats.loc["MJ-2025-000003", "preparer_volume"] == 3
    assert "AJ-2025-000001" not in feats.index
