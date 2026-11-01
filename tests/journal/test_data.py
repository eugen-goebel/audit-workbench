"""Tests for loading the journal files and the views the rules share."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from audit_workbench.core.evidence import EvidenceError
from audit_workbench.journal.data import AS_OF, JournalData, load_journal_data
from tests.journal.builders import MiniLedger


def test_decimals_are_parsed_exactly(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="1234.50")
    ledger.entry("MJ-2025-000002", amount="0.10")
    ledger.entry("MJ-2025-000003", amount="19999999.99")
    ledger.entry("MJ-2025-000004", lines=[("6000", "7", ""), ("1600", "", "7.0"), ("1000", "", "")])
    data = ledger.load(tmp_path)

    entries = data.entries.set_index("doc_no")
    assert entries["amount"].to_dict() == {
        "MJ-2025-000001": Decimal("1234.50"),
        "MJ-2025-000002": Decimal("0.10"),
        "MJ-2025-000003": Decimal("19999999.99"),
        "MJ-2025-000004": Decimal("7.00"),
    }
    assert entries["amount_cents"].to_dict()["MJ-2025-000003"] == 1_999_999_999
    assert entries["amount_cents"].dtype.kind == "i"
    # lines carry exact integer cents and Decimals, never floats
    first = data.lines.iloc[0]
    assert first["debit"] == Decimal("1234.50")
    assert first["debit_cents"] == 123450
    assert first["amount_cents"] == 123450  # the side that is not zero


def test_amount_is_total_debit_over_all_lines(tmp_path):
    ledger = MiniLedger()
    ledger.entry(
        "MJ-2025-000001",
        lines=[("6000", "60.00", ""), ("6000", "40.00", ""), ("1600", "", "100.00")],
    )
    data = ledger.load(tmp_path)
    assert data.entries.at[0, "amount_cents"] == 10000


def test_unbalanced_entry_raises_and_names_doc_no(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="100.00")
    ledger.entry("MJ-2025-000002", lines=[("6000", "100.00", ""), ("1600", "", "99.99")])
    ledger.entry("MJ-2025-000003", lines=[("6000", "50.00", ""), ("1600", "", "50.01")])
    with pytest.raises(EvidenceError) as exc:
        ledger.load(tmp_path)
    message = str(exc.value)
    assert "MJ-2025-000002" in message
    assert "MJ-2025-000003" in message
    assert "MJ-2025-000001" not in message


@pytest.mark.parametrize("bad", ["12,50", "1e3", "-5.00", "1.234", "abc"])
def test_malformed_amount_raises(tmp_path, bad):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", lines=[("6000", bad, ""), ("1600", "", bad)])
    with pytest.raises(EvidenceError, match="debit|credit"):
        ledger.load(tmp_path)


def test_malformed_dates_and_doc_numbers_raise(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", posting_date="2025-13-40")
    with pytest.raises(EvidenceError, match="posting_date"):
        ledger.load(tmp_path / "a")

    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", entry_ts="not a timestamp")
    with pytest.raises(EvidenceError, match="entry_ts"):
        ledger.load(tmp_path / "b")

    ledger = MiniLedger()
    ledger.entry("MJ-2025-1", number_range="MJ-2025")
    with pytest.raises(EvidenceError, match="doc_no"):
        ledger.load(tmp_path / "c")

    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", number_range="AJ-2025")
    with pytest.raises(EvidenceError, match="number_range"):
        ledger.load(tmp_path / "d")

    ledger = MiniLedger()
    ledger.limit("manual", "lots")
    with pytest.raises(EvidenceError, match="limit"):
        ledger.load(tmp_path / "e")


def test_preparer_and_approver_map_to_employees(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E2")
    ledger.account("GL-E2", "E2")
    ledger.account("GL-E2B", "E2")  # second GL account of the same employee
    ledger.entry("MJ-2025-000001", preparer="GL-E2", approver="GL-E2B")
    ledger.entry("MJ-2025-000002", preparer="GL-E1", approver="")
    ledger.entry("AJ-2025-000001", source="automatic")
    data = ledger.load(tmp_path)

    entries = data.entries.set_index("doc_no")
    assert entries.at["MJ-2025-000001", "preparer_employee_id"] == "E2"
    assert entries.at["MJ-2025-000001", "approver_employee_id"] == "E2"
    assert entries.at["MJ-2025-000002", "preparer_employee_id"] == "E1"
    assert entries.at["MJ-2025-000002", "approver_employee_id"] is None
    assert entries.at["AJ-2025-000001", "preparer_employee_id"] is None  # technical account


def test_timestamps_and_dates_are_typed(tmp_path):
    ledger = MiniLedger()
    ledger.entry(
        "MJ-2025-000001",
        posting_date="2025-03-10",
        entry_ts="2025-03-11T08:30:15",
        approver="GL-E1",
        approved_ts="2025-03-12T09:00:00",
    )
    ledger.entry("MJ-2025-000002")
    data = ledger.load(tmp_path)
    row = data.entries.iloc[0]
    assert row["posting_date"] == date(2025, 3, 10)
    assert row["entry_ts"].hour == 8
    assert row["approved_ts"].day == 12
    assert pd.isna(data.entries.iloc[1]["approved_ts"])


def test_views(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="100.00")
    ledger.entry("AJ-2025-000001", source="automatic", amount="300.00")
    ledger.entry("MJ-2025-000002", amount="5.00")
    ledger.header("MJ-2025-000002")  # duplicate header
    data = ledger.load(tmp_path)

    assert len(data.entries) == 4
    assert len(data.unique_entries) == 3
    assert sorted(data.manual_entries["doc_no"]) == ["MJ-2025-000001", "MJ-2025-000002"]
    assert data.close_dates["2025-03"] == date(2025, 4, 10)
    assert data.manual_limit_cents == 10_000_000
    assert data.gl_accounts.at["4000", "account_class"] == "income"
    assert bool(data.gl_accounts.at["4000", "manual_posting"]) is True
    assert data.employees.at["E1", "org_unit"] == "Rechnungswesen"

    # the line view carries what the Benford tests need, once per doc_no
    assert len(data.lines) == 6  # lines are as in the file
    pop = data.amount_lines
    assert set(pop["source"]) == {"manual", "automatic"}
    assert len(pop) == 6  # the duplicate header does not duplicate lines
    assert set(pop["preparer_employee_id"].dropna()) == {"E1"}

    pairs = data.pair_counts
    assert pairs[("6000", "1600")] == 3


def test_missing_manual_limit_raises_when_used(tmp_path):
    ledger = MiniLedger()
    ledger.rows["approval_limits"] = []
    ledger.entry("MJ-2025-000001")
    data = ledger.load(tmp_path)
    with pytest.raises(EvidenceError, match="manual"):
        _ = data.manual_limit_cents


def test_hashes(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    data = ledger.load(tmp_path)
    h = data.hashes("journal_entries", "journal_lines")
    assert len(h) == 2 and all(len(x) == 64 for x in h)


def test_load_journal_data_defaults_as_of(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    data = load_journal_data(ledger.write(tmp_path))
    assert isinstance(data, JournalData)
    assert data.as_of == AS_OF == date(2026, 2, 28)
    assert load_journal_data(tmp_path, as_of=date(2026, 1, 1)).as_of == date(2026, 1, 1)


def test_missing_file_raises(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    ledger.write(tmp_path)
    (tmp_path / "accounts.csv").unlink()
    with pytest.raises(EvidenceError, match="accounts"):
        load_journal_data(tmp_path)


def test_empty_entry_timestamp_raises(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", entry_ts="")
    with pytest.raises(EvidenceError, match="entry_ts: empty"):
        ledger.load(tmp_path)


def test_invalid_manual_posting_flag_raises(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    ledger.rows["gl_accounts"][0]["manual_posting"] = "yes"
    with pytest.raises(EvidenceError, match="manual_posting"):
        ledger.load(tmp_path)


def test_unknown_source_raises(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    ledger.rows["journal_entries"][0]["source"] = "interface"
    with pytest.raises(EvidenceError, match="source"):
        ledger.load(tmp_path)


def test_header_without_lines_has_amount_zero(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    ledger.rows["journal_lines"] = []
    data = ledger.load(tmp_path)
    assert data.entries.at[0, "amount_cents"] == 0
    assert data.entries.at[0, "amount"] == Decimal("0.00")
    assert data.entry_pairs["MJ-2025-000001"] == frozenset()
