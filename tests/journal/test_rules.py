"""Tests for journal-entry rules B-01 to B-10.

Each rule starts with the positive case and then the near misses of spec
section 4, so a change that makes a rule too eager or too lazy fails here.

Near misses that must NOT be flagged (spec section 4):
  B-01  600 amounts with a high MAD (below min_n); conforming preparer
  B-02  conforming account; fewer than min_n amounts
  B-03  15 % round share; exactly 25 %; fewer than min_entries
  B-04  automatic entries on weekends; 24 and 31 December
  B-05  entry on the close date itself; reversal on day 11
  B-06  number range change MJ-2025 -> MJ-2026
  B-07  monthly rent (30 days apart); beyond window_days; reversal pair
  B-08  one cent below the limit without approver
  B-09  rare pair without income/equity/suspense; pair frequency base
  B-10  accountants in the posting unit
"""

from __future__ import annotations

import numpy as np
import pytest

from audit_workbench.core.evidence import EvidenceError
from audit_workbench.core.findings import Severity
from audit_workbench.journal.rules import (
    JOURNAL_RULES,
    ApprovalMissing,
    BenfordFirstDigit,
    BenfordFirstTwo,
    DocNumberCompleteness,
    DuplicateEntries,
    OffHours,
    PeriodEnd,
    RoundAmounts,
    UnexpectedPreparer,
    UnusualAccountPair,
    run_journal_rules,
)
from tests.journal.builders import MiniLedger


def _fmt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _benford_amounts(n: int) -> list[str]:
    """A stratified sample of log-uniform amounts 10 ... 10,000,000 (conforms to Benford)."""
    u = (np.arange(n) + 0.5) / n
    return [_fmt(int(c)) for c in np.round(10 ** (1 + 6 * u) * 100)]


def _entities(findings):
    return [f.entity for f in findings]


# ===========================================================================
# B-01  benford_first_digit
# ===========================================================================


def _invented(ledger: MiniLedger, entries: int, **kw) -> None:
    """Entries whose amounts all start with 5 (a preparer who invents numbers)."""
    for i in range(entries):
        ledger.entry(amount=f"5{i % 90 + 10}.{i % 100:02d}", **kw)


def test_b01_flags_preparer_with_invented_amounts(tmp_path):
    ledger = MiniLedger()
    _invented(ledger, 500)  # 1000 line amounts
    findings = BenfordFirstDigit().run(ledger.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.entity, f.severity) == ("B-01", "E1", Severity.MEDIUM)
    assert f.details["n"] == 1000
    assert f.details["mad"] > 0.015
    assert f.details["conformity"] == "nonconformity"
    assert {"chi_square", "digits"} <= set(f.details)
    assert len(f.details["digits"]) == 9
    assert f.details["digits"][4]["count"] == 1000
    assert f.params["min_n"] == 1000


def test_b01_min_n_boundary(tmp_path):
    ledger = MiniLedger()
    _invented(ledger, 499)  # 998 amounts: one short of min_n
    data = ledger.load(tmp_path / "a")
    assert BenfordFirstDigit().run(data) == []
    assert _entities(BenfordFirstDigit(min_n=998).run(data)) == ["E1"]
    assert BenfordFirstDigit(min_n=999).run(data) == []


def test_b01_small_sample_with_high_mad_is_not_flagged(tmp_path):
    ledger = MiniLedger()
    _invented(ledger, 300)  # 600 amounts, MAD far above 0.015
    assert BenfordFirstDigit().run(ledger.load(tmp_path)) == []


def test_b01_conforming_preparer_is_not_flagged(tmp_path):
    ledger = MiniLedger()
    for amount in _benford_amounts(600):
        ledger.entry(amount=amount)
    assert BenfordFirstDigit().run(ledger.load(tmp_path)) == []


def test_b01_groups_by_employee_across_gl_accounts(tmp_path):
    ledger = MiniLedger()
    ledger.account("GL-E1B", "E1")  # a second GL account of the same employee
    _invented(ledger, 250, preparer="GL-E1")
    _invented(ledger, 250, preparer="GL-E1B")
    assert _entities(BenfordFirstDigit().run(ledger.load(tmp_path))) == ["E1"]


def test_b01_ignores_automatic_entries_and_amounts_below_ten(tmp_path):
    ledger = MiniLedger()
    _invented(ledger, 600, source="automatic")
    for i in range(600):
        ledger.entry(amount=f"5.{i % 100:02d}")
    assert BenfordFirstDigit(min_n=1).run(ledger.load(tmp_path)) == []


def test_b01_skips_manual_entries_of_unmapped_accounts(tmp_path):
    ledger = MiniLedger()
    ledger.account("GL-SYS")  # technical account used for a manual entry: no employee
    _invented(ledger, 20, preparer="GL-SYS")
    assert BenfordFirstDigit(min_n=1).run(ledger.load(tmp_path)) == []


# ===========================================================================
# B-02  benford_first_two
# ===========================================================================


def _uniform_two_digit(ledger: MiniLedger, entries: int, *, automatic_every: int = 2) -> None:
    """Amounts 10.00-99.99 with uniformly distributed first two digits on account 4000."""
    for i in range(entries):
        cents = (10 + i % 90) * 100 + (i * 37) % 100
        source = "automatic" if i % automatic_every == 0 else "manual"
        ledger.entry(amount=_fmt(cents), debit="4000", credit=f"C{i % 50:02d}", source=source)


def test_b02_flags_account_with_uniform_amounts(tmp_path):
    ledger = MiniLedger()
    _uniform_two_digit(ledger, 5000)  # all sources count
    findings = BenfordFirstTwo().run(ledger.load(tmp_path))

    assert _entities(findings) == ["4000"]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-02", Severity.MEDIUM)
    assert f.details["n"] == 5000
    assert f.details["mad"] > 0.0022
    assert len(f.details["digits"]) == 90
    assert f.params["min_n"] == 5000


def test_b02_min_n_boundary(tmp_path):
    ledger = MiniLedger()
    _uniform_two_digit(ledger, 4999)
    data = ledger.load(tmp_path)
    assert BenfordFirstTwo().run(data) == []
    assert _entities(BenfordFirstTwo(min_n=4999).run(data)) == ["4000"]


def test_b02_conforming_account_is_not_flagged(tmp_path):
    ledger = MiniLedger()
    for i, amount in enumerate(_benford_amounts(5000)):
        ledger.entry(amount=amount, debit="4000", credit=f"C{i % 50:02d}")
    assert BenfordFirstTwo().run(ledger.load(tmp_path)) == []


# ===========================================================================
# B-03  round_amounts
# ===========================================================================


def _round_mix(ledger: MiniLedger, total: int, round_ones: int, **kw) -> None:
    for i in range(total):
        amount = f"{(i % 9 + 1) * 1000}.00" if i < round_ones else f"{1234 + i}.56"
        ledger.entry(amount=amount, **kw)


def test_b03_flags_preparer_with_many_round_amounts(tmp_path):
    ledger = MiniLedger()
    _round_mix(ledger, 40, 14)  # 35 %
    findings = RoundAmounts().run(ledger.load(tmp_path))

    assert len(findings) == 1
    f = findings[0]
    assert (f.rule_id, f.entity, f.severity) == ("B-03", "E1", Severity.LOW)
    assert f.details["entries"] == 40
    assert f.details["round_entries"] == 14
    assert f.details["share"] == pytest.approx(0.35)
    assert f.params == {"min_entries": 30, "round_share": 0.25, "as_of": "2026-02-28"}


def test_b03_fifteen_percent_is_not_flagged(tmp_path):
    ledger = MiniLedger()
    _round_mix(ledger, 40, 6)
    assert RoundAmounts().run(ledger.load(tmp_path)) == []


def test_b03_share_must_be_strictly_above_threshold(tmp_path):
    ledger = MiniLedger()
    _round_mix(ledger, 40, 10)  # exactly 25 %
    assert RoundAmounts().run(ledger.load(tmp_path / "a")) == []
    ledger = MiniLedger()
    _round_mix(ledger, 40, 11)
    assert _entities(RoundAmounts().run(ledger.load(tmp_path / "b"))) == ["E1"]


def test_b03_min_entries_boundary(tmp_path):
    ledger = MiniLedger()
    _round_mix(ledger, 29, 29)
    assert RoundAmounts().run(ledger.load(tmp_path / "a")) == []
    ledger = MiniLedger()
    _round_mix(ledger, 30, 30)
    assert _entities(RoundAmounts().run(ledger.load(tmp_path / "b"))) == ["E1"]


def test_b03_only_amounts_from_1000_count(tmp_path):
    ledger = MiniLedger()
    for _ in range(30):
        ledger.entry(amount="999.00")  # round-looking but below 1000.00: not in population
    for i in range(10):
        ledger.entry(amount="1000.00" if i == 0 else f"{1500 + i}.00")
    data = ledger.load(tmp_path)
    assert RoundAmounts().run(data) == []  # only 10 entries >= 1000.00
    f = RoundAmounts(min_entries=10, round_share=0.05).run(data)[0]
    assert (f.details["entries"], f.details["round_entries"]) == (10, 1)


def test_b03_uses_total_debit_and_counts_employee_not_account(tmp_path):
    ledger = MiniLedger()
    ledger.account("GL-E1B", "E1")
    for i in range(20):
        ledger.entry(amount="2000.00", preparer="GL-E1")
        ledger.entry(
            lines=[("6000", "1000.00", ""), ("6000", "1000.00", ""), ("1600", "", "2000.00")],
            preparer="GL-E1B",
        )
        ledger.entry(amount=f"{3000 + i}.50", preparer="GL-E1B")
    findings = RoundAmounts().run(ledger.load(tmp_path))
    assert _entities(findings) == ["E1"]
    assert findings[0].details["entries"] == 60


def test_b03_automatic_entries_are_not_counted(tmp_path):
    ledger = MiniLedger()
    for _ in range(40):
        ledger.entry(amount="5000.00", source="automatic")
    assert RoundAmounts().run(ledger.load(tmp_path)) == []


def test_b03_params_can_be_overridden(tmp_path):
    ledger = MiniLedger()
    _round_mix(ledger, 10, 6)
    data = ledger.load(tmp_path)
    assert RoundAmounts().run(data) == []
    assert _entities(RoundAmounts(min_entries=10, round_share=0.5).run(data)) == ["E1"]


# ===========================================================================
# B-04  off_hours
# ===========================================================================


def _off_hours(tmp_path, **entry_kwargs):
    ledger = MiniLedger()
    doc = ledger.entry(**entry_kwargs)
    return doc, OffHours().run(ledger.load(tmp_path))


def test_b04_flags_saturday(tmp_path):
    doc, findings = _off_hours(tmp_path, entry_ts="2025-03-08T10:00:00")
    assert _entities(findings) == [doc]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-04", Severity.MEDIUM)
    assert f.details["weekday"] == "Saturday"
    assert f.details["holiday"] is None
    assert f.details["entry_ts"] == "2025-03-08T10:00:00"


def test_b04_flags_sunday(tmp_path):
    doc, findings = _off_hours(tmp_path, entry_ts="2025-03-09T22:15:00")
    assert _entities(findings) == [doc]
    assert findings[0].details["weekday"] == "Sunday"


@pytest.mark.parametrize(
    ("day", "name"),
    [
        ("2025-06-09", "Pfingstmontag"),  # Whit Monday
        ("2025-06-19", "Fronleichnam"),  # a Hessen holiday
        ("2025-10-03", "Tag der Deutschen Einheit"),
        ("2025-12-25", "Erster Weihnachtstag"),
        ("2026-01-01", "Neujahr"),  # January 2026 is in the extract
    ],
)
def test_b04_flags_public_holidays_in_hessen(tmp_path, day, name):
    doc, findings = _off_hours(tmp_path, entry_ts=f"{day}T09:30:00")
    assert _entities(findings) == [doc]
    assert findings[0].details["holiday"] == name


def test_b04_holiday_details_carry_the_weekday(tmp_path):
    _, findings = _off_hours(tmp_path, entry_ts="2025-06-09T09:30:00")
    assert findings[0].details["weekday"] == "Monday"
    assert findings[0].details["holiday"] == "Pfingstmontag"


@pytest.mark.parametrize("day", ["2025-12-24", "2025-12-31"])
def test_b04_christmas_eve_and_new_years_eve_are_working_days(tmp_path, day):
    _, findings = _off_hours(tmp_path, entry_ts=f"{day}T10:00:00")
    assert findings == []


@pytest.mark.parametrize("day", ["2025-10-31", "2026-01-06"])
def test_b04_holidays_of_other_states_are_not_flagged(tmp_path, day):
    _, findings = _off_hours(tmp_path, entry_ts=f"{day}T10:00:00")
    assert findings == []


def test_b04_automatic_weekend_batches_are_not_flagged(tmp_path):
    _, findings = _off_hours(tmp_path, source="automatic", entry_ts="2025-03-08T02:00:00")
    assert findings == []


def test_b04_uses_entry_timestamp_not_posting_date(tmp_path):
    # posted on a Saturday, but keyed on Wednesday
    _, findings = _off_hours(tmp_path, posting_date="2025-03-08", entry_ts="2025-03-05T10:00:00")
    assert findings == []
    # posted for Monday, keyed on Saturday
    doc, findings = _off_hours(
        tmp_path / "b", posting_date="2025-03-10", entry_ts="2025-03-08T10:00:00"
    )
    assert _entities(findings) == [doc]


def test_b04_day_boundaries(tmp_path):
    ledger = MiniLedger()
    ledger.entry(entry_ts="2025-03-07T23:59:59")  # Friday
    sat_early = ledger.entry(entry_ts="2025-03-08T00:00:00")
    ledger.entry(entry_ts="2025-03-10T00:00:00")  # Monday
    sun_late = ledger.entry(entry_ts="2025-03-09T23:59:59")
    assert sorted(_entities(OffHours().run(ledger.load(tmp_path)))) == sorted([sat_early, sun_late])


# ===========================================================================
# B-05  period_end
# ===========================================================================


def test_b05_post_closing_entry(tmp_path):
    ledger = MiniLedger()
    doc = ledger.entry(posting_date="2025-03-20", entry_ts="2025-04-11T09:00:00")
    findings = PeriodEnd().run(ledger.load(tmp_path))

    assert _entities(findings) == [doc]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-05", Severity.HIGH)
    assert f.details == {
        "triggers": ["post_closing"],
        "close_date": "2025-04-10",
        "days_after_close": 1,
        "reversed_by": None,
    }
    assert f.params["end_days"] == 5 and f.params["reversal_days"] == 10


def test_b05_entry_on_the_close_date_itself_is_not_flagged(tmp_path):
    ledger = MiniLedger()
    ledger.entry(posting_date="2025-03-20", entry_ts="2025-04-10T00:00:00")
    ledger.entry(posting_date="2025-03-20", entry_ts="2025-04-10T23:59:59")
    assert PeriodEnd().run(ledger.load(tmp_path)) == []


def test_b05_days_after_close_counts_calendar_days(tmp_path):
    ledger = MiniLedger()
    ledger.entry(posting_date="2025-03-20", entry_ts="2025-05-02T09:00:00")
    assert PeriodEnd().run(ledger.load(tmp_path))[0].details["days_after_close"] == 22


def test_b05_early_reversal(tmp_path):
    ledger = MiniLedger()
    orig = ledger.entry(posting_date="2025-03-28", entry_ts="2025-03-28T10:00:00")
    rev = ledger.entry(
        posting_date="2025-04-03",
        entry_ts="2025-04-03T10:00:00",
        reversal_of=orig,
        debit="1600",
        credit="6000",
    )
    findings = PeriodEnd().run(ledger.load(tmp_path))

    assert _entities(findings) == [orig]  # the entity is the original, not the reversal
    f = findings[0]
    assert f.severity == Severity.MEDIUM
    assert f.details == {
        "triggers": ["early_reversal"],
        "close_date": "2025-04-10",
        "days_after_close": None,
        "reversed_by": rev,
    }


@pytest.mark.parametrize(("reversal_day", "flagged"), [(1, True), (10, True), (11, False)])
def test_b05_reversal_window_in_next_period(tmp_path, reversal_day, flagged):
    ledger = MiniLedger()
    orig = ledger.entry(posting_date="2025-03-30")
    ledger.entry(posting_date=f"2025-04-{reversal_day:02d}", reversal_of=orig)
    assert (_entities(PeriodEnd().run(ledger.load(tmp_path))) == [orig]) is flagged


@pytest.mark.parametrize(("posting_day", "flagged"), [(26, False), (27, True), (31, True)])
def test_b05_last_five_days_of_the_period(tmp_path, posting_day, flagged):
    ledger = MiniLedger()
    orig = ledger.entry(posting_date=f"2025-03-{posting_day:02d}")
    ledger.entry(posting_date="2025-04-02", reversal_of=orig)
    assert (_entities(PeriodEnd().run(ledger.load(tmp_path))) == [orig]) is flagged


def test_b05_reversal_in_same_period_or_two_periods_later_is_not_early(tmp_path):
    ledger = MiniLedger()
    a = ledger.entry(posting_date="2025-03-28")
    ledger.entry(posting_date="2025-03-30", reversal_of=a)  # same period
    b = ledger.entry(posting_date="2025-04-29")
    ledger.entry(posting_date="2025-06-02", reversal_of=b)  # two periods later
    assert PeriodEnd().run(ledger.load(tmp_path)) == []


def test_b05_reversal_across_the_year_end(tmp_path):
    ledger = MiniLedger()
    orig = ledger.entry(posting_date="2025-12-30")
    rev = ledger.entry(posting_date="2026-01-05", reversal_of=orig)
    findings = PeriodEnd().run(ledger.load(tmp_path))
    assert _entities(findings) == [orig]
    assert findings[0].details["reversed_by"] == rev


def test_b05_february_uses_the_real_month_length(tmp_path):
    ledger = MiniLedger()
    a = ledger.entry(posting_date="2025-02-24")  # 28 - 5 + 1 = 24: inside the last 5 days
    ledger.entry(posting_date="2025-03-02", reversal_of=a)
    b = ledger.entry(posting_date="2025-02-23")
    ledger.entry(posting_date="2025-03-02", reversal_of=b)
    assert _entities(PeriodEnd().run(ledger.load(tmp_path))) == [a]


def test_b05_both_triggers_make_it_high(tmp_path):
    ledger = MiniLedger()
    orig = ledger.entry(posting_date="2025-03-30", entry_ts="2025-04-15T09:00:00")
    rev = ledger.entry(posting_date="2025-04-02", reversal_of=orig)
    f = PeriodEnd().run(ledger.load(tmp_path))[0]
    assert f.severity == Severity.HIGH
    assert f.details["triggers"] == ["post_closing", "early_reversal"]
    assert f.details["reversed_by"] == rev
    assert f.details["days_after_close"] == 5


def test_b05_automatic_entries_are_not_tested(tmp_path):
    ledger = MiniLedger()
    ledger.entry(source="automatic", posting_date="2025-03-20", entry_ts="2025-04-20T01:00:00")
    assert PeriodEnd().run(ledger.load(tmp_path)) == []


def test_b05_automatic_reversal_of_a_manual_entry_counts(tmp_path):
    ledger = MiniLedger()
    orig = ledger.entry(posting_date="2025-03-30")
    rev = ledger.entry(source="automatic", posting_date="2025-04-01", reversal_of=orig)
    assert PeriodEnd().run(ledger.load(tmp_path))[0].details["reversed_by"] == rev


def test_b05_period_without_close_date_cannot_be_post_closing(tmp_path):
    ledger = MiniLedger()
    ledger.entry(posting_date="2026-05-05", entry_ts="2026-05-06T00:00:00")
    assert PeriodEnd().run(ledger.load(tmp_path)) == []


def test_b05_params_can_be_overridden(tmp_path):
    ledger = MiniLedger()
    orig = ledger.entry(posting_date="2025-03-22")
    ledger.entry(posting_date="2025-04-15", reversal_of=orig)
    data = ledger.load(tmp_path)
    assert PeriodEnd().run(data) == []
    assert _entities(PeriodEnd(end_days=10, reversal_days=15).run(data)) == [orig]


# ===========================================================================
# B-06  doc_number_completeness
# ===========================================================================


def _numbered(ledger: MiniLedger, number_range: str, counters, **kw) -> None:
    for c in counters:
        ledger.entry(f"{number_range}-{c:06d}", **kw)


def test_b06_gap_entity_is_zero_padded_like_the_doc_no(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", [1, 2, 5, 6])
    findings = DocNumberCompleteness().run(ledger.load(tmp_path))

    assert _entities(findings) == ["MJ-2025:gap:000003-000004"]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-06", Severity.HIGH)
    assert f.details["kind"] == "gap"
    assert f.details["number_range"] == "MJ-2025"
    assert f.details["first_missing"] == "MJ-2025-000003"
    assert f.details["last_missing"] == "MJ-2025-000004"
    assert f.details["missing_count"] == 2


def test_b06_single_missing_counter_and_padding_of_large_counters(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", [1, 3, 99, 101, 999, 1001])
    entities = _entities(DocNumberCompleteness().run(ledger.load(tmp_path)))
    assert entities == [
        "MJ-2025:gap:000002-000002",
        "MJ-2025:gap:000004-000098",
        "MJ-2025:gap:000100-000100",
        "MJ-2025:gap:000102-000998",
        "MJ-2025:gap:001000-001000",
    ]


def test_b06_consecutive_missing_counters_form_one_gap(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "AJ-2025", [10, 11, 20], source="automatic")
    findings = DocNumberCompleteness().run(ledger.load(tmp_path))
    assert _entities(findings) == ["AJ-2025:gap:000012-000019"]
    assert findings[0].details["missing_count"] == 8


def test_b06_only_counters_between_lowest_and_highest_count(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", [7, 8, 9])  # the range does not have to start at 1
    assert DocNumberCompleteness().run(ledger.load(tmp_path)) == []


def test_b06_range_change_is_not_a_gap(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", [1, 2, 3])
    _numbered(ledger, "MJ-2026", [1, 2], posting_date="2026-01-05", entry_ts="2026-01-05T10:00:00")
    assert DocNumberCompleteness().run(ledger.load(tmp_path)) == []


def test_b06_ranges_are_checked_independently_and_include_automatic(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", [1, 2, 4])
    _numbered(ledger, "AJ-2025", [1, 3], source="automatic")
    findings = DocNumberCompleteness().run(ledger.load(tmp_path))
    assert _entities(findings) == ["AJ-2025:gap:000002-000002", "MJ-2025:gap:000003-000003"]


def test_b06_duplicate_doc_no(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", [1, 2, 3])
    ledger.header("MJ-2025-000002")
    findings = DocNumberCompleteness().run(ledger.load(tmp_path))
    assert _entities(findings) == ["MJ-2025:dup:MJ-2025-000002"]
    f = findings[0]
    assert (f.severity, f.details["kind"], f.details["occurrences"]) == (Severity.HIGH, "dup", 2)


def test_b06_triplicate_is_one_finding_and_duplicate_is_not_a_gap(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", [1, 2])
    ledger.header("MJ-2025-000001").header("MJ-2025-000001")
    findings = DocNumberCompleteness().run(ledger.load(tmp_path))
    assert _entities(findings) == ["MJ-2025:dup:MJ-2025-000001"]
    assert findings[0].details["occurrences"] == 3


def test_b06_complete_range_has_no_findings(tmp_path):
    ledger = MiniLedger()
    _numbered(ledger, "MJ-2025", range(1, 50))
    assert DocNumberCompleteness().run(ledger.load(tmp_path)) == []


# ===========================================================================
# B-07  duplicate_entries
# ===========================================================================


def test_b07_flags_the_later_entry(tmp_path):
    ledger = MiniLedger()
    first = ledger.entry(
        amount="4711.00", posting_date="2025-03-10", entry_ts="2025-03-10T09:00:00"
    )
    second = ledger.entry(
        amount="4711.00", posting_date="2025-03-12", entry_ts="2025-03-12T09:00:00"
    )
    findings = DuplicateEntries().run(ledger.load(tmp_path))

    assert _entities(findings) == [second]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-07", Severity.MEDIUM)
    assert f.details["duplicate_of"] == [first]
    assert f.details["days_apart"] == 2
    assert f.params["window_days"] == 7


def test_b07_later_is_decided_by_entry_ts_then_doc_no(tmp_path):
    ledger = MiniLedger()
    # doc 000001 was keyed after 000002
    a = ledger.entry("MJ-2025-000001", amount="50.00", entry_ts="2025-03-11T12:00:00")
    ledger.entry("MJ-2025-000002", amount="50.00", entry_ts="2025-03-10T12:00:00")
    assert _entities(DuplicateEntries().run(ledger.load(tmp_path / "x"))) == [a]
    # same entry_ts: the higher doc_no is the later one
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="50.00", entry_ts="2025-03-10T12:00:00")
    d = ledger.entry("MJ-2025-000002", amount="50.00", entry_ts="2025-03-10T12:00:00")
    assert _entities(DuplicateEntries().run(ledger.load(tmp_path / "y"))) == [d]


@pytest.mark.parametrize(("days", "flagged"), [(0, True), (7, True), (8, False)])
def test_b07_window_in_days(tmp_path, days, flagged):
    ledger = MiniLedger()
    ledger.entry(amount="900.00", posting_date="2025-03-10")
    later = ledger.entry(amount="900.00", posting_date=f"2025-03-{10 + days:02d}")
    assert (_entities(DuplicateEntries().run(ledger.load(tmp_path))) == [later]) is flagged


def test_b07_monthly_rent_is_not_a_duplicate(tmp_path):
    ledger = MiniLedger()
    for month in range(1, 13):
        ledger.entry(
            amount="8500.00",
            debit="6000",
            credit="1000",
            posting_date=f"2025-{month:02d}-01",
            description="Rent",
        )
    assert DuplicateEntries().run(ledger.load(tmp_path)) == []


def test_b07_thirty_days_apart_is_outside_the_window(tmp_path):
    ledger = MiniLedger()
    ledger.entry(amount="8500.00", posting_date="2025-03-01")
    ledger.entry(amount="8500.00", posting_date="2025-03-31")
    assert DuplicateEntries().run(ledger.load(tmp_path)) == []


def test_b07_reversal_pair_is_not_a_duplicate(tmp_path):
    ledger = MiniLedger()
    orig = ledger.entry(amount="900.00", posting_date="2025-03-10")
    ledger.entry(amount="900.00", posting_date="2025-03-11", reversal_of=orig)
    assert DuplicateEntries().run(ledger.load(tmp_path)) == []


def test_b07_different_amount_or_lines_are_not_duplicates(tmp_path):
    ledger = MiniLedger()
    ledger.entry(amount="900.00")
    ledger.entry(amount="900.01")
    ledger.entry(amount="900.00", debit="1200")  # other account
    ledger.entry(amount="900.00", credit="1000")
    ledger.entry(  # same amount and accounts but a different split
        lines=[("6000", "400.00", ""), ("6000", "500.00", ""), ("1600", "", "900.00")]
    )
    assert DuplicateEntries().run(ledger.load(tmp_path)) == []


def test_b07_line_order_does_not_matter(tmp_path):
    ledger = MiniLedger()
    ledger.entry(lines=[("6000", "400.00", ""), ("1200", "500.00", ""), ("1600", "", "900.00")])
    later = ledger.entry(
        lines=[("1600", "", "900.00"), ("1200", "500.00", ""), ("6000", "400.00", "")]
    )
    assert _entities(DuplicateEntries().run(ledger.load(tmp_path))) == [later]


def test_b07_three_copies_flag_the_second_and_third(tmp_path):
    ledger = MiniLedger()
    first = ledger.entry(amount="77.00", posting_date="2025-03-10", entry_ts="2025-03-10T10:00:00")
    second = ledger.entry(amount="77.00", posting_date="2025-03-11", entry_ts="2025-03-11T10:00:00")
    third = ledger.entry(amount="77.00", posting_date="2025-03-12", entry_ts="2025-03-12T10:00:00")
    findings = DuplicateEntries().run(ledger.load(tmp_path))
    assert _entities(findings) == [second, third]
    assert findings[1].details["duplicate_of"] == [first, second]


def test_b07_automatic_entries_and_duplicate_headers_are_ignored(tmp_path):
    ledger = MiniLedger()
    ledger.entry(amount="10.00", source="automatic")
    ledger.entry(amount="10.00", source="automatic")
    single = ledger.entry(amount="20.00")
    ledger.header(single)  # same doc_no twice is B-06's business, not a B-07 duplicate
    assert DuplicateEntries().run(ledger.load(tmp_path)) == []


def test_b07_window_can_be_overridden(tmp_path):
    ledger = MiniLedger()
    ledger.entry(amount="900.00", posting_date="2025-03-01")
    later = ledger.entry(amount="900.00", posting_date="2025-03-31")
    assert _entities(DuplicateEntries(window_days=30).run(ledger.load(tmp_path))) == [later]


# ===========================================================================
# B-08  approval_missing
# ===========================================================================


def test_b08_missing_approver_at_the_limit(tmp_path):
    ledger = MiniLedger()
    doc = ledger.entry(amount="100000.00")
    findings = ApprovalMissing().run(ledger.load(tmp_path))

    assert _entities(findings) == [doc]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-08", Severity.HIGH)
    assert f.details["trigger"] == "missing"
    assert f.details["preparer_employee_id"] == "E1"
    assert f.details["approver_employee_id"] is None
    assert f.details["amount"] == "100000.00"
    assert f.details["limit"] == "100000.00"


def test_b08_one_cent_below_the_limit_is_not_flagged(tmp_path):
    ledger = MiniLedger()
    ledger.entry(amount="99999.99")
    assert ApprovalMissing().run(ledger.load(tmp_path)) == []


def test_b08_amount_is_the_total_debit(tmp_path):
    ledger = MiniLedger()
    doc = ledger.entry(
        lines=[("6000", "60000.00", ""), ("6000", "40000.00", ""), ("1600", "", "100000.00")]
    )
    assert _entities(ApprovalMissing().run(ledger.load(tmp_path))) == [doc]


def test_b08_approved_by_another_employee_is_fine(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E2")
    ledger.account("GL-E2", "E2")
    ledger.entry(amount="500000.00", approver="GL-E2", approved_ts="2025-03-10T11:00:00")
    assert ApprovalMissing().run(ledger.load(tmp_path)) == []


def test_b08_self_approval_with_the_same_account(tmp_path):
    ledger = MiniLedger()
    doc = ledger.entry(amount="150000.00", approver="GL-E1", approved_ts="2025-03-10T11:00:00")
    f = ApprovalMissing().run(ledger.load(tmp_path))[0]
    assert f.entity == doc
    assert f.details["trigger"] == "self_approval"
    assert f.details["preparer_employee_id"] == f.details["approver_employee_id"] == "E1"


def test_b08_self_approval_across_two_gl_accounts_of_one_employee(tmp_path):
    ledger = MiniLedger()
    ledger.account("GL-E1B", "E1")  # the second GL account of the same employee
    doc = ledger.entry(
        amount="250000.00", preparer="GL-E1", approver="GL-E1B", approved_ts="2025-03-10T11:00:00"
    )
    other = ledger.entry(
        amount="250000.00", preparer="GL-E1B", approver="GL-E1", approved_ts="2025-03-10T11:00:00"
    )
    findings = ApprovalMissing().run(ledger.load(tmp_path))
    assert _entities(findings) == [doc, other]
    assert {f.details["trigger"] for f in findings} == {"self_approval"}
    assert findings[0].details["preparer_employee_id"] == "E1"


def test_b08_small_entries_without_approver_are_not_tested(tmp_path):
    ledger = MiniLedger()
    ledger.entry(amount="10.00")
    ledger.entry(amount="99999.99", approver="GL-E1")
    assert ApprovalMissing().run(ledger.load(tmp_path)) == []


def test_b08_automatic_entries_are_not_tested(tmp_path):
    ledger = MiniLedger()
    ledger.entry(amount="9000000.00", source="automatic")
    assert ApprovalMissing().run(ledger.load(tmp_path)) == []


def test_b08_approver_account_without_employee_counts_as_approved(tmp_path):
    ledger = MiniLedger()
    ledger.account("GL-SYS")  # technical account: cannot be compared to a person
    ledger.entry(amount="200000.00", approver="GL-SYS")
    ledger.entry(amount="200000.00", approver="GL-UNKNOWN")
    assert ApprovalMissing().run(ledger.load(tmp_path)) == []


def test_b08_uses_the_manual_limit_from_the_file(tmp_path):
    ledger = MiniLedger()
    ledger.limit("manual", "5000.00")
    ledger.limit("automatic", "999999999.00")
    doc = ledger.entry(amount="5000.00")
    ledger.entry(amount="4999.99")
    assert _entities(ApprovalMissing().run(ledger.load(tmp_path))) == [doc]


def test_b08_without_a_manual_limit_the_rule_cannot_run(tmp_path):
    ledger = MiniLedger()
    ledger.rows["approval_limits"] = []
    ledger.entry(amount="1.00")
    with pytest.raises(EvidenceError, match="manual"):
        ApprovalMissing().run(ledger.load(tmp_path))


# ===========================================================================
# B-09  unusual_account_pair
# ===========================================================================


def test_b09_rare_pair_with_income_account(tmp_path):
    ledger = MiniLedger()
    doc = ledger.entry(
        amount="1500.00", debit="1200", credit="4000"
    )  # receivables / interest income
    findings = UnusualAccountPair().run(ledger.load(tmp_path))

    assert _entities(findings) == [doc]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-09", Severity.MEDIUM)
    assert f.details["pairs"] == [{"debit_account": "1200", "credit_account": "4000", "entries": 1}]
    assert f.params["max_count"] == 3


@pytest.mark.parametrize("account", ["3000", "9999", "4000"])
def test_b09_equity_suspense_and_income_are_involved_accounts(tmp_path, account):
    ledger = MiniLedger()
    d1 = ledger.entry(debit=account, credit="1000")
    d2 = ledger.entry(debit="1000", credit=account)
    assert _entities(UnusualAccountPair().run(ledger.load(tmp_path))) == [d1, d2]


@pytest.mark.parametrize("account", ["1000", "1600", "6000"])
def test_b09_rare_pair_of_asset_liability_expense_is_not_flagged(tmp_path, account):
    ledger = MiniLedger()
    ledger.entry(debit=account, credit="1200")
    assert UnusualAccountPair().run(ledger.load(tmp_path)) == []


def test_b09_pair_frequency_counts_entries_of_the_whole_year(tmp_path):
    ledger = MiniLedger()
    ledger.entry(debit="1200", credit="4000", posting_date="2025-02-01")  # manual, tested
    ledger.entry(debit="1200", credit="4000", source="automatic")  # counts for the base
    assert len(UnusualAccountPair().run(ledger.load(tmp_path / "a"))) == 1  # 2 < 3

    ledger.entry(debit="1200", credit="4000", source="automatic", posting_date="2025-11-01")
    assert UnusualAccountPair().run(ledger.load(tmp_path / "b")) == []  # 3 is not fewer than 3


def test_b09_automatic_entries_are_not_tested(tmp_path):
    ledger = MiniLedger()
    ledger.entry(debit="1200", credit="4000", source="automatic")
    assert UnusualAccountPair().run(ledger.load(tmp_path)) == []


def test_b09_every_debit_credit_combination_is_a_pair(tmp_path):
    ledger = MiniLedger()
    for _ in range(5):  # (1200, 1000) is common
        ledger.entry(debit="1200", credit="1000")
        ledger.entry(debit="1200", credit="1000", source="automatic")
    doc = ledger.entry(
        lines=[
            ("1200", "100.00", ""),
            ("1200", "50.00", ""),  # same account twice: still one entry for the pair
            ("1000", "", "100.00"),
            ("4000", "", "50.00"),
        ]
    )
    findings = UnusualAccountPair().run(ledger.load(tmp_path))
    assert _entities(findings) == [doc]
    # only the pair that is rare AND involves an income account is reported
    assert findings[0].details["pairs"] == [
        {"debit_account": "1200", "credit_account": "4000", "entries": 1}
    ]


def test_b09_unknown_account_is_not_an_involved_class(tmp_path):
    ledger = MiniLedger()
    ledger.entry(debit="1200", credit="7777")
    assert UnusualAccountPair().run(ledger.load(tmp_path)) == []


def test_b09_max_count_can_be_overridden(tmp_path):
    ledger = MiniLedger()
    for _ in range(3):
        ledger.entry(debit="1200", credit="4000")
    data = ledger.load(tmp_path)
    assert UnusualAccountPair().run(data) == []
    assert len(UnusualAccountPair(max_count=4).run(data)) == 3


# ===========================================================================
# B-10  unexpected_preparer
# ===========================================================================


def test_b10_preparer_outside_posting_units(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E2", org_unit="Zahlungsverkehr")
    ledger.account("GL-E2", "E2")
    doc = ledger.entry(preparer="GL-E2")
    ledger.entry(preparer="GL-E1")  # Rechnungswesen: fine
    findings = UnexpectedPreparer().run(ledger.load(tmp_path))

    assert _entities(findings) == [doc]
    f = findings[0]
    assert (f.rule_id, f.severity) == ("B-10", Severity.HIGH)
    assert f.details == {
        "triggers": ["org_unit"],
        "preparer_employee_id": "E2",
        "org_unit": "Zahlungsverkehr",
        "job_function": "Sachbearbeitung",
    }
    assert f.params["posting_units"] == ["Rechnungswesen"]


def test_b10_administrator_inside_the_posting_unit(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E3", org_unit="Rechnungswesen", job_function="Administration")
    ledger.account("GL-E3", "E3")
    doc = ledger.entry(preparer="GL-E3")
    f = UnexpectedPreparer().run(ledger.load(tmp_path))[0]
    assert f.entity == doc
    assert f.details["triggers"] == ["job_function"]


def test_b10_it_administrator_triggers_both(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E4", org_unit="IT", job_function="Administration")
    ledger.account("GL-E4", "E4")
    ledger.entry(preparer="GL-E4")
    f = UnexpectedPreparer().run(ledger.load(tmp_path))[0]
    assert f.details["triggers"] == ["org_unit", "job_function"]


def test_b10_posting_units_can_be_overridden(tmp_path):
    ledger = MiniLedger()
    ledger.employee("E2", org_unit="Zahlungsverkehr")
    ledger.account("GL-E2", "E2")
    ledger.entry(preparer="GL-E2")
    ledger.entry(preparer="GL-E1")
    data = ledger.load(tmp_path)
    assert UnexpectedPreparer(posting_units=["Rechnungswesen", "Zahlungsverkehr"]).run(data) == []
    assert len(UnexpectedPreparer(posting_units=["Controlling"]).run(data)) == 2


def test_b10_automatic_entries_and_unmapped_accounts_are_skipped(tmp_path):
    ledger = MiniLedger()
    ledger.entry(source="automatic")
    ledger.account("GL-SYS")
    ledger.entry(preparer="GL-SYS")  # no employee behind the account
    ledger.entry(preparer="GL-UNKNOWN")  # not in accounts.csv
    ledger.account("GL-E9", "E9")  # employee not in HR
    ledger.entry(preparer="GL-E9")
    assert UnexpectedPreparer().run(ledger.load(tmp_path)) == []


# ===========================================================================
# Registry and run_journal_rules
# ===========================================================================


def test_registry_contains_b01_to_b10():
    assert JOURNAL_RULES.ids() == [f"B-{i:02d}" for i in range(1, 11)]
    assert JOURNAL_RULES.get("B-08") is ApprovalMissing
    for rule_id in JOURNAL_RULES.ids():
        cls = JOURNAL_RULES.get(rule_id)
        assert cls.version and cls.title
        assert cls.__doc__


def test_clean_ledger_has_no_findings(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="1234.56")
    ledger.entry("MJ-2025-000002", amount="55.00", debit="1200", credit="1000")
    ledger.entry("AJ-2025-000001", source="automatic", entry_ts="2025-03-08T02:00:00")
    assert run_journal_rules(ledger.load(tmp_path)) == []


def test_run_journal_rules_is_sorted_and_deterministic(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="300000.00", entry_ts="2025-03-08T10:00:00")  # B-04, B-08
    ledger.entry("MJ-2025-000003", amount="12.00", debit="1200", credit="4000")  # B-09, B-06 gap
    data = ledger.load(tmp_path)

    first = run_journal_rules(data)
    second = run_journal_rules(data)
    assert [f.finding_id for f in first] == [f.finding_id for f in second]
    keys = [(f.rule_id, f.entity) for f in first]
    assert keys == sorted(keys)
    assert keys == [
        ("B-04", "MJ-2025-000001"),
        ("B-06", "MJ-2025:gap:000002-000002"),
        ("B-08", "MJ-2025-000001"),
        ("B-09", "MJ-2025-000003"),
    ]
    for f in first:
        assert f.rule_version == "1.0.0"
        assert f.params["as_of"] == "2026-02-28"
        assert f.regulation_refs
        assert all(len(h) == 64 for h in f.evidence)


def test_run_journal_rules_accepts_overrides_per_rule(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="1200.00", debit="1200", credit="1000")
    data = ledger.load(tmp_path)
    assert run_journal_rules(data) == []

    findings = run_journal_rules(
        data, **{"B-08": {}, "B-03": {"min_entries": 1, "round_share": 0.0}}
    )
    assert findings == []  # 1200.00 is not a multiple of 1000.00

    ledger.entry("MJ-2025-000002", amount="3000.00", debit="1200", credit="1000")
    findings = run_journal_rules(
        ledger.load(tmp_path / "b"), **{"B-03": {"min_entries": 2, "round_share": 0.4}}
    )
    assert [(f.rule_id, f.entity) for f in findings] == [("B-03", "E1")]
    assert findings[0].params["min_entries"] == 2


def test_run_journal_rules_accepts_a_params_mapping(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="3000.00", debit="1200", credit="1000")
    ledger.entry("MJ-2025-000002", amount="2000.00", debit="1200", credit="1000")
    data = ledger.load(tmp_path)
    findings = run_journal_rules(data, {"B-03": {"min_entries": 2}})
    assert [f.rule_id for f in findings] == ["B-03"]


def test_run_journal_rules_rejects_unknown_rule_ids(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001")
    with pytest.raises(KeyError, match="B-99"):
        run_journal_rules(ledger.load(tmp_path), **{"B-99": {}})


def test_b07_entry_without_lines_is_ignored(tmp_path):
    ledger = MiniLedger()
    ledger.entry("MJ-2025-000001", amount="100.00")
    ledger.entry("MJ-2025-000002", amount="100.00")
    ledger.rows["journal_lines"] = []
    data = ledger.load(tmp_path)
    assert DuplicateEntries().run(data) == []
