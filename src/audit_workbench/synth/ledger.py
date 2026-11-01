"""Deterministic generator of the module B journal ledger for "Musterbank AG".

The ledger belongs to the same synthetic bank as the access data
(``synth/bank.py``). Every human GL user is a ``GL-`` account of an employee
from ``hr_employees.csv``; automatic batches are posted by an existing
technical GL account. The module is driven by the department's own random
stream, ``numpy.random.default_rng([seed, 2])``, so the module A output stays
byte-identical.

Structure (spec ``docs/design/module-b-journal.md`` section 4)
--------------------------------------------------------------

* Fiscal year 2025 (with the January/February 2026 postings of the extract up
  to ``2026-02-28``), one number range per source and year (``MJ-2025``,
  ``AJ-2025``, ``MJ-2026``, ``AJ-2026``).
* About 60,000 entries and about 180,000 lines, about a fifth of the entries
  manual. Automatic batches run every night, weekends included.
* Manual amounts are log-uniform over whole decades 10.00 - 10,000,000.00
  (exactly Benford distributed). The uniforms are stratified per preparer
  and the automatic amounts over the whole population, so the first-digit and
  first-two-digit distributions of every group stay tight and B-01/B-02 do
  not fire on the clean ledger.
* About 5 % of the manual entries are round amounts (all segments whole
  thousands, floor of a log-uniform draw, so they stay Benford conforming),
  spread evenly over the preparers.
* Account pairs come from a fixed template set (single segments plus fixed
  compound templates), so every real pair occurs often - in particular every
  pair with an income, equity or suspense account.
* No duplicate entry signature within seven days, no manual entry on a
  weekend or Hessen public holiday, nothing posted after the close of its
  period, four-eyes approval for every manual entry at or above the limit.

``build_ledger(bank)`` produces the clean ledger, ``inject_journal(bank,
ledger)`` adds exactly the 42 issues of spec section 4 and
``check_clean_ledger(ledger)`` is the independent self check that makes
``generate_bank`` retry with another stream if an honest group would trip a
threshold.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import holidays
import numpy as np

from audit_workbench.synth.bank import Bank

JOURNAL_AS_OF = date(2026, 2, 28)
YEAR_START = date(2025, 1, 1)
YEAR_END = date(2025, 12, 31)
FISCAL_YEAR = 2025
#: approval limit for manual entries (cents)
MANUAL_LIMIT = 10_000_000
AUTO_LIMIT = 50_000_000

MIN_AMOUNT = 1_000  # 10.00
MAX_AMOUNT = 1_000_000_000  # 10,000,000.00
ROUND_STEP = 100_000  # 1,000.00

BENFORD_FIRST = np.log10(1.0 + 1.0 / np.arange(1, 10))
BENFORD_FIRST_MAD_MAX = 0.015
BENFORD_FIRST_MIN_N = 1000
BENFORD_TWO_MAD_MAX = 0.0022
BENFORD_TWO_MIN_N = 5000


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------

HE_HOLIDAYS = holidays.Germany(subdiv="HE", years=[2025, 2026])


def is_holiday(day: date) -> bool:
    return day in HE_HOLIDAYS


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def is_workday(day: date) -> bool:
    return not is_weekend(day) and not is_holiday(day)


def next_workday(day: date) -> date:
    while not is_workday(day):
        day += timedelta(days=1)
    return day


def workdays(start: date, end: date) -> list[date]:
    out = []
    day = start
    while day <= end:
        if is_workday(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def month_end(month: str) -> date:
    year, mon = (int(p) for p in month.split("-"))
    if mon == 12:
        return date(year, 12, 31)
    return date(year, mon + 1, 1) - timedelta(days=1)


def _fifth_workday(year: int, month: int) -> date:
    return workdays(date(year, month, 1), month_end(f"{year}-{month:02d}"))[4]


def close_of(period: str) -> date:
    """Fifth workday of the month after *period* - the lock date of the period."""
    year, month = (int(p) for p in period.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return _fifth_workday(year, month)


PERIODS = [f"{FISCAL_YEAR}-{m:02d}" for m in range(1, 13)]
CLOSE_CALENDAR: list[tuple[str, date]] = [(period, close_of(period)) for period in PERIODS]
CLOSE_BY_PERIOD = dict(CLOSE_CALENDAR)


# Dates used by the near misses and by the injections.
#: German Unity Day (public holiday), Whit Monday is added from ``holidays``.
HOLIDAY_A = date(2025, 10, 3)
WEEKEND_DAYS = [
    date(2025, 3, 15),
    date(2025, 8, 24),
    date(2025, 11, 1),
    date(2026, 2, 7),
]


# ---------------------------------------------------------------------------
# chart of accounts
# ---------------------------------------------------------------------------

# account_no, name, account_class, manual_posting
CHART_OF_ACCOUNTS: list[tuple[str, str, str, bool]] = [
    # assets
    ("1000", "Kasse", "asset", True),
    ("1010", "Guthaben bei der Deutschen Bundesbank", "asset", False),
    ("1020", "Schuldverschreibungen oeffentlicher Emittenten", "asset", False),
    ("1100", "Nostro-Guthaben bei Kreditinstituten", "asset", False),
    ("1110", "Nostro-Guthaben USD", "asset", False),
    ("1120", "Nostro-Guthaben GBP", "asset", False),
    ("1130", "Nostro-Guthaben CHF", "asset", False),
    ("1200", "Termingelder bei Kreditinstituten", "asset", False),
    ("1210", "Pensionsgeschafte Aktiva", "asset", False),
    ("1300", "Forderungen an Kunden - taeglich faellige Kontokorrentkredite", "asset", False),
    ("1310", "Forderungen an Kunden - Kontokorrentkonten", "asset", False),
    ("1320", "Forderungen an Kunden - Hypothekendarlehen", "asset", False),
    ("1330", "Forderungen an Kunden - Konsumentenkredite", "asset", False),
    ("1340", "Forderungen an Kunden - Firmenkredite", "asset", False),
    ("1350", "Forderungen an Kunden - Kommunalkredite", "asset", False),
    ("1360", "Forderungen an Kunden - Leasingforderungen", "asset", False),
    ("1500", "Zinsforderungen an Kunden", "asset", False),
    ("1510", "Gebuehrenforderungen an Kunden", "asset", False),
    ("1600", "Wertberichtigungen Forderungen an Kunden", "asset", True),
    ("1700", "Beteiligungen", "asset", True),
    ("1750", "Sonstige Finanzanlagen", "asset", True),
    ("1800", "Forderungen an verbundene Unternehmen", "asset", True),
    ("1810", "Forderungen an Beteiligungsunternehmen", "asset", True),
    ("1900", "Sachanlagen", "asset", True),
    ("1910", "Betriebs- und Geschaeftsausstattung", "asset", True),
    ("1920", "Gebaeude", "asset", True),
    ("1930", "Anlagen im Bau", "asset", True),
    ("1990", "Kumulierte Abschreibungen", "asset", True),
    ("2100", "Sonstige Vermoegensgegenstaende", "asset", True),
    ("2200", "Aktive latente Steuern", "asset", True),
    ("2300", "Steuererstattungsansprueche", "asset", True),
    ("2400", "Rechnungsabgrenzungsposten", "asset", True),
    ("2500", "Treuhandvermoegen", "asset", True),
    # liabilities
    ("3000", "Verbindlichkeiten gegenueber Kreditinstituten", "liability", False),
    ("3010", "Taeglich faellige Verbindlichkeiten an Kreditinstitute", "liability", False),
    ("3020", "Verbindlichkeiten gegenueber der Deutschen Bundesbank", "liability", False),
    ("3030", "Pensionsgeschafte Passiva", "liability", False),
    ("3200", "Verbindlichkeiten gegenueber Kunden - Spareinlagen", "liability", False),
    ("3201", "Verbindlichkeiten gegenueber Kunden - Termineinlagen", "liability", False),
    ("3202", "Verbindlichkeiten gegenueber Kunden - Festgelder", "liability", False),
    ("3203", "Verbindlichkeiten gegenueber Kunden - Sichteinlagen Privat", "liability", False),
    ("3204", "Verbindlichkeiten gegenueber Kunden - Sichteinlagen Firmen", "liability", False),
    ("3205", "Verbindlichkeiten gegenueber Kunden - Kontokorrent Firmen", "liability", False),
    ("3206", "Verbindlichkeiten gegenueber Kunden - Kontokorrent Privat", "liability", False),
    ("3300", "Begebene Schuldverschreibungen", "liability", False),
    ("3400", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability", True),
    ("3500", "Zinsabgrenzung Einlagen", "liability", True),
    ("3600", "Verbindlichkeiten gegenueber verbundenen Unternehmen", "liability", True),
    ("3700", "Sonstige Verbindlichkeiten", "liability", True),
    ("3710", "Verbindlichkeiten Personal", "liability", True),
    ("3720", "Verbindlichkeiten soziale Sicherheit", "liability", True),
    ("3730", "Umsatzsteuer", "liability", True),
    ("3740", "Lohn- und Kirchensteuer", "liability", True),
    ("3750", "Verbindlichkeiten aus Treuhandvermoegen", "liability", True),
    ("3800", "Rueckstellungen Personal", "liability", True),
    ("3810", "Rueckstellungen Pensionen", "liability", True),
    ("3820", "Rueckstellungen Steuern", "liability", True),
    ("3830", "Rueckstellungen Rechtsrisiken", "liability", True),
    ("3840", "Rueckstellungen Pruefungskosten", "liability", True),
    ("3850", "Rueckstellungen Kreditrisiko", "liability", True),
    ("3900", "Passive latente Steuern", "liability", True),
    ("3910", "Passive Rechnungsabgrenzung", "liability", True),
    ("3920", "Nachrangige Verbindlichkeiten", "liability", True),
    # equity
    ("4000", "Gezeichnetes Kapital", "equity", True),
    ("4010", "Kapitalruecklage", "equity", True),
    ("4020", "Gewinnruecklagen", "equity", True),
    ("4030", "Gesetzliche Ruecklage", "equity", True),
    ("4040", "Ruecklage fuer allgemeine Bankrisiken", "equity", True),
    ("4050", "Bilanzgewinn / Bilanzverlust", "equity", True),
    ("4060", "Gewinnvortrag", "equity", True),
    ("4070", "Verlustvortrag", "equity", True),
    # income
    ("4100", "Zinsertraege Kredit- und Geldmarktgeschaeft", "income", True),
    ("4110", "Zinsertraege Wertpapiere", "income", True),
    ("4120", "Zinsertraege Kreditinstitute", "income", True),
    ("4200", "Ertraege aus Beteiligungen", "income", True),
    ("4300", "Provisionsertraege Zahlungsverkehr", "income", True),
    ("4310", "Provisionsertraege Wertpapiergeschaeft", "income", True),
    ("4320", "Provisionsertraege Kontofuehrung", "income", True),
    ("4330", "Provisionsertraege Kreditgeschaeft", "income", True),
    ("4340", "Provisionsertraege Kartengeschaeft", "income", True),
    ("4400", "Handelsergebnis", "income", True),
    ("4410", "Ergebnis aus Waehrungsumrechnung", "income", True),
    ("4500", "Sonstige betriebliche Ertraege", "income", True),
    ("4510", "Ertraege aus Vermietung", "income", True),
    ("4520", "Ertraege aus Aufloesung von Rueckstellungen", "income", True),
    ("4530", "Ertraege aus Zuschreibungen", "income", True),
    # expenses
    ("5000", "Zinsaufwendungen Einlagen", "expense", False),
    ("5010", "Zinsaufwendungen Kreditinstitute", "expense", False),
    ("5020", "Zinsaufwendungen begebene Schuldverschreibungen", "expense", False),
    ("5030", "Zinsaufwendungen nachrangige Verbindlichkeiten", "expense", False),
    ("5100", "Personalaufwendungen", "expense", True),
    ("5110", "Sozialabgaben", "expense", True),
    ("5120", "Aufwendungen Altersvorsorge", "expense", True),
    ("5200", "Raumkosten", "expense", True),
    ("5210", "Nebenkosten Raum", "expense", True),
    ("5300", "Buerobedarf", "expense", True),
    ("5310", "EDV-Kosten", "expense", True),
    ("5320", "Softwarepflege", "expense", True),
    ("5400", "Rechts- und Beratungskosten", "expense", True),
    ("5410", "Pruefungskosten", "expense", True),
    ("5420", "Werbekosten", "expense", True),
    ("5430", "Reisekosten", "expense", True),
    ("5440", "Versicherungen", "expense", True),
    ("5450", "Beitraege", "expense", True),
    ("5460", "Telekommunikation", "expense", True),
    ("5470", "Fortbildung", "expense", True),
    ("5500", "Abschreibungen Sachanlagen", "expense", True),
    ("5600", "Aufwendungen aus Wertberichtigungen", "expense", True),
    ("5610", "Abschreibungen Finanzanlagen", "expense", True),
    ("5700", "Sonstige betriebliche Aufwendungen", "expense", True),
    ("5710", "Spenden und Sponsoring", "expense", True),
    ("5800", "Steuern vom Einkommen und Ertrag", "expense", True),
    # suspense
    ("9990", "Durchlaufkonto Zahlungsverkehr", "suspense", True),
    ("9995", "Verrechnungskonto", "suspense", True),
    ("9998", "Zwischenkonto", "suspense", True),
]

ACCOUNT_CLASS = {no: cls for no, _n, cls, _m in CHART_OF_ACCOUNTS}
MANUAL_OK = {no for no, _n, _c, m in CHART_OF_ACCOUNTS if m}
ACCOUNT_NAME = {no: name for no, name, _c, _m in CHART_OF_ACCOUNTS}

FEE_CLEARING = ["9990", "9995", "9998"]

# manual single-segment templates: (debit, credit, weight)
MANUAL_SINGLES: list[tuple[str, str, int]] = [
    ("9990", "1100", 5),
    ("1100", "9990", 3),
    ("9995", "1000", 4),
    ("1000", "9995", 3),
    ("9998", "1100", 3),
    ("1100", "9998", 2),
    ("2100", "9990", 3),
    ("3700", "3400", 6),
    ("3400", "1100", 6),
    ("1900", "3400", 2),
    ("1910", "3400", 2),
    ("5310", "3400", 3),
    ("5400", "3400", 1),
    ("5430", "3400", 1),
    ("5440", "3400", 1),
    ("5300", "3400", 1),
    ("5100", "3710", 6),
    ("5110", "3720", 3),
    ("5120", "3810", 1),
    ("3730", "1100", 2),
    ("3740", "1100", 1),
    ("5800", "3730", 1),
    ("5700", "1100", 4),
    ("5700", "3700", 2),
    ("5700", "3400", 2),
    ("5700", "3710", 1),
    ("3830", "3700", 1),
    ("3830", "1100", 1),
    ("2400", "5700", 3),
    ("5700", "2400", 3),
    ("3910", "5700", 2),
    ("5710", "1100", 1),
    ("1510", "4300", 2),
    ("1510", "4310", 1),
    ("1510", "4320", 2),
    ("1510", "4330", 1),
    ("1510", "4340", 1),
    ("1510", "4500", 2),
    ("1500", "4100", 4),
    ("1500", "4120", 1),
    ("1600", "4100", 2),
    ("1600", "4400", 1),
    ("5610", "1750", 1),
    ("1100", "4410", 1),
    ("4400", "1700", 1),
    ("4020", "4050", 2),
    ("4030", "4050", 1),
    ("4040", "4050", 1),
    ("1510", "4310", 1),
    ("1800", "4500", 1),
    ("4100", "1500", 2),
    ("9995", "3700", 2),
    ("1900", "1600", 1),
]

# manual compound templates: (debits, credits, weight); fixed account
# combinations only, so every pair including the cross pairs stays frequent.
MANUAL_COMPOUNDS: list[tuple[list[str], list[str], int]] = [
    (["3400", "5310"], ["3700", "1100"], 2),
    (["3710", "3400"], ["3720", "3740"], 1),
    (["5110", "5100", "5120"], ["3710", "3720", "1100"], 1),
    (["5700", "5310"], ["1100", "3700"], 2),
    (["5430", "5400"], ["1100", "3400"], 1),
    (["1500", "1600"], ["4100", "4120"], 1),
    (["1510", "1500"], ["4300", "4340"], 1),
    (["3400", "1900"], ["3700", "1100"], 2),
    (["5310", "5320"], ["3400", "1100"], 1),
    (["5700", "3830"], ["1100", "3700"], 1),
]

# manual templates that may be reversed: the flipped pair also occurs often
# (it is a regular manual template).
REVERSIBLE = [
    ("5700", "3400"),
    ("5310", "3400"),
    ("5700", "1100"),
]

# automatic templates: (debits, credits, weight)
AUTO_SINGLES: list[tuple[str, str, int]] = [
    ("3206", "1100", 10),  # private payment out
    ("1100", "3206", 10),  # private payment in
    ("3205", "1100", 8),  # corporate payment out
    ("1100", "3205", 8),  # corporate payment in
    ("1000", "3206", 2),  # cash withdrawal
    ("3206", "1000", 2),  # cash deposit
    ("1300", "1100", 6),  # loan disbursement
    ("1100", "1300", 6),  # loan repayment
    ("1330", "1100", 3),
    ("1100", "1330", 3),
    ("1340", "1100", 3),
    ("1100", "1340", 3),
    ("1500", "4100", 8),  # interest accrual
    ("3200", "4100", 6),
    ("3201", "4100", 4),
    ("5000", "3500", 6),
    ("5010", "3000", 3),
    ("5020", "3300", 2),
    ("1100", "3200", 5),  # savings deposit
    ("3200", "1100", 5),  # savings withdrawal
    ("1100", "3202", 4),
    ("3202", "1100", 4),
    ("1200", "3201", 2),
    ("3000", "1200", 2),
    ("1020", "1100", 2),  # securities purchase for own book
    ("1100", "1020", 2),  # securities sale
    ("1500", "4310", 3),  # securities fee
    ("1510", "4340", 3),  # card fee
    ("1510", "4330", 2),  # lending fee
    ("1600", "4100", 3),  # risk provision
    ("5500", "1990", 1),  # depreciation
    ("3730", "1100", 1),  # VAT payment
    ("9998", "1100", 2),
    ("1100", "9998", 2),
]

# The clearing accounts of the nightly fee batches (counter side stays below
# the B-02 sample threshold).
FEE_POOL = FEE_CLEARING
#: fee income account fed by the nightly card fee batches (B-02 population)
FEE_ACCOUNT = "4340"
#: fee lines per night
FEE_SEGMENTS = 12

# rare pairs used by the B-09 injection: (debit, credit)
RARE_PAIRS = [
    ("1800", "4120"),
    ("1810", "4120"),
    ("3400", "4330"),
    ("2200", "4500"),
    ("1700", "4030"),
]

# postings on a public holiday used by the B-04 injection
#: Whit Monday 2025 and German Unity Day 2025
B04_HOLIDAYS = [date(2025, 6, 9), HOLIDAY_A]

SPEC_COUNTS = {
    "B-01": 1,
    "B-02": 1,
    "B-03": 2,
    "B-04": 6,
    "B-05": 8,
    "B-06": 4,
    "B-07": 5,
    "B-08": 6,
    "B-09": 5,
    "B-10": 4,
}


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Segment:
    """One balanced debit/credit pair of an entry, in cents."""

    debit: str
    credit: str
    amount: int


@dataclass(slots=True)
class Entry:
    kind: str  # manual | auto
    entry_day: date
    entry_time: str  # HH:MM:SS
    posting_day: date
    preparer: str  # account id
    segments: list[Segment]
    description: str
    reversible: bool = False
    #: approval behaviour: "auto" (normal), "missing", "self"
    approval: str = "auto"
    #: entry marked by an injection, later steps must not touch it
    frozen: bool = False
    #: entry must keep its exact amount (duplicate cases, rent, ...)
    protected: bool = False
    reversal_of: str = ""
    approver: str = ""
    #: account that must approve the entry (a second account of the preparer for
    #: ``approval == "self"``, or the approver of a near miss of the X rules)
    approver_account: str = ""
    approved_ts: str = ""
    doc_no: str = ""
    number_range: str = ""
    rnd: float = 0.5
    rnd_pick: float = 0.0
    #: ground truth hints attached by the injection
    note: str = ""

    @property
    def ts(self) -> str:
        return f"{self.entry_day.isoformat()}T{self.entry_time}"

    @property
    def total(self) -> int:
        return sum(s.amount for s in self.segments)

    def signature(self) -> tuple:
        return tuple(sorted((s.debit, s.credit, s.amount) for s in self.segments))


@dataclass
class Ledger:
    people: People = field(default=None)  # type: ignore[assignment]
    entries: list[Entry] = field(default_factory=list)
    approvers: list[str] = field(default_factory=list)
    clerks: list[str] = field(default_factory=list)
    close_periods: tuple[str, ...] = ()
    occasional: list[str] = field(default_factory=list)
    #: entries that are removed after numbering (a B-06 gap, by run)
    gap_runs: list[list[Entry]] = field(default_factory=list)
    #: entry whose header is written twice after numbering (a B-06 duplicate)
    duplicate: Entry | None = None
    issues: list[dict[str, str]] = field(default_factory=list)
    #: entries removed from the extract (a B-06 gap)
    removed: list[Entry] = field(default_factory=list)


def cents_text(amount: int) -> str:
    return f"{amount // 100}.{amount % 100:02d}"


def leading_digits(amounts: list[int] | np.ndarray, digits: int = 1) -> np.ndarray:
    """First *digits* significant digits of integer cent amounts."""
    a = np.asarray(amounts, dtype=np.int64)
    if a.size == 0:
        return np.zeros(0, dtype=np.int64)
    exp = np.floor(np.log10(a))
    first = a // (10 ** (exp.astype(np.int64) - (digits - 1)))
    return first


def mad_first(amounts: list[int] | np.ndarray) -> float:
    """Nigrini MAD of the first digit against Benford."""
    a = np.asarray(amounts, dtype=np.int64)
    a = a[a >= MIN_AMOUNT]
    if a.size == 0:
        return 9.0
    first = leading_digits(a, 1)
    observed = np.bincount(first - 1, minlength=9) / a.size
    return float(np.mean(np.abs(observed - BENFORD_FIRST)))


def mad_first_two(amounts: list[int] | np.ndarray) -> float:
    a = np.asarray(amounts, dtype=np.int64)
    a = a[a >= MIN_AMOUNT]
    if a.size == 0:
        return 9.0
    first = leading_digits(a, 2)
    observed = np.bincount(first - 10, minlength=90) / a.size
    digits = np.arange(10, 100)
    expected = np.log10(1.0 + 1.0 / digits)
    return float(np.mean(np.abs(observed - expected)))


def stratified_log_uniform(n: int, rng: np.random.Generator) -> np.ndarray:
    """Log-uniform over whole decades, one stratum per draw.

    ``log10`` of a log-uniform draw over the decades has independent integer
    and fractional parts. Drawing the fractional part from its own stratum
    keeps every sample's fractional parts nearly evenly spread, so the leading
    digits of even a small sample are close to Benford. Without stratification
    a sample of 1,200 amounts can exceed the MAD threshold by chance.
    """
    frac = (np.arange(n) + rng.random(n)) / n
    # 10^(1..6) * frac -> 10.00 .. 10,000,000.00 in cents, whole decades
    exp = rng.integers(1, 7, size=n) if n else np.zeros(0, dtype=np.int64)
    value = 10.0 ** (exp + frac)
    return np.clip(np.rint(value * 100.0), MIN_AMOUNT, MAX_AMOUNT).astype(np.int64)


def round_thousands(amounts: np.ndarray) -> np.ndarray:
    """Whole thousands of the same log-uniform draw (Benford preserving)."""
    out = (amounts // ROUND_STEP) * ROUND_STEP
    return np.maximum(out, ROUND_STEP)


# ---------------------------------------------------------------------------
# people and accounts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class People:
    """HR and GL account lookup of the synthetic bank."""

    entry_date: dict[str, date]
    exit_date: dict[str, date | None]
    org_unit: dict[str, str]
    job_function: dict[str, str]
    personal: dict[str, str]  # employee -> first personal GL account
    accounts: dict[str, str]  # account -> employee

    @classmethod
    def from_bank(cls, bank: Bank) -> People:
        entry: dict[str, date] = {}
        exit_: dict[str, date | None] = {}
        unit: dict[str, str] = {}
        func: dict[str, str] = {}
        for row in sorted(bank.rows["hr_employees"], key=lambda r: r["employee_id"]):
            emp = row["employee_id"]
            entry[emp] = date.fromisoformat(row["entry_date"])
            exit_[emp] = date.fromisoformat(row["exit_date"]) if row["exit_date"] else None
            unit[emp] = row["org_unit"]
            func[emp] = row["job_function"]
        personal: dict[str, str] = {}
        accounts: dict[str, str] = {}
        for row in sorted(bank.rows["accounts"], key=lambda r: r["account_id"]):
            emp = row["employee_id"]
            if row["system"] != "GL" or not emp or emp not in entry:
                continue
            accounts[row["account_id"]] = emp
            if row["account_type"] == "personal" and emp not in personal:
                personal[emp] = row["account_id"]
        return cls(entry, exit_, unit, func, personal, accounts)

    def employed(self, emp: str, day: date) -> bool:
        if emp not in self.entry_date:
            return False
        if day < self.entry_date[emp]:
            return False
        exit_day = self.exit_date.get(emp)
        return exit_day is None or exit_day > day

    def employee_of(self, account: str) -> str:
        return self.accounts.get(account, "")

    def eligible(self, emp: str, start: date = YEAR_START, end: date = JOURNAL_AS_OF) -> bool:
        return self.employed(emp, start) and self.employed(emp, end)


def technical_accounts(bank: Bank) -> list[str]:
    """Existing technical GL accounts of the clean bank (created 2024-06-01)."""
    return [
        row["account_id"]
        for row in bank.rows["accounts"]
        if row["system"] == "GL"
        and row["account_type"] == "technical"
        and not row["employee_id"]
        and row["created_at"].startswith("2024-")
    ]


# ---------------------------------------------------------------------------
# clean ledger
# ---------------------------------------------------------------------------


#: compounds get a higher weight than the table says: real manual journals
#: transfer several accounts at once (about 1.5 segments per entry).
COMPOUND_WEIGHT_FACTOR = 4


def _draw_kind(rng: np.random.Generator) -> tuple[str, tuple]:
    """One manual entry template: a single segment or a compound."""
    total = sum(w for *_x, w in MANUAL_SINGLES) + COMPOUND_WEIGHT_FACTOR * sum(
        w for *_x, w in MANUAL_COMPOUNDS
    )
    draw = int(rng.integers(0, total))
    for debit, credit, weight in MANUAL_SINGLES:
        if draw < weight:
            return ("single", (debit, credit))
        draw -= weight
    for debits, credits, weight in MANUAL_COMPOUNDS:
        weight *= COMPOUND_WEIGHT_FACTOR
        if draw < weight:
            return ("compound", (tuple(debits), tuple(credits)))
        draw -= weight
    raise AssertionError("unreachable")


def _segments_of(kind: tuple, amounts: np.ndarray) -> list[Segment]:
    if kind[0] == "single":
        return [Segment(kind[1][0], kind[1][1], int(amounts[0]))]
    debits, credits = kind[1]
    count = max(len(debits), len(credits))
    return [
        Segment(
            debits[i % len(debits)],
            credits[i % len(credits)],
            int(amounts[i]),
        )
        for i in range(count)
    ]


def _segment_count(kind: tuple) -> int:
    if kind[0] == "single":
        return 1
    return max(len(kind[1][0]), len(kind[1][1]))


def _to_round(entry: Entry) -> None:
    for seg in entry.segments:
        seg.amount = max((seg.amount // ROUND_STEP) * ROUND_STEP, ROUND_STEP)


def _assign_amounts(
    plan: list[tuple[str, tuple]], rng: np.random.Generator, round_share: float
) -> list[np.ndarray]:
    """Log-uniform amounts for every entry of one preparer, plus round share.

    *round_share* refers to the entries with an amount of at least 1,000.00,
    the population of B-03.
    """
    counts = np.array([_segment_count(kind) for kind in plan], dtype=np.int64)
    pool = stratified_log_uniform(int(counts.sum()) + 32, rng)
    out: list[np.ndarray] = []
    pos = 0
    for count in counts:
        out.append(pool[pos : pos + count])
        pos += int(count)
    # Round amounts: about ``round_ratio`` of the entries, taken from the
    # eligible ones (single segment, at least 1,000.00) in random order.
    eligible = [i for i, kind in enumerate(plan) if _segment_count(kind) == 1]
    group = [i for i in eligible if out[i][0] >= ROUND_STEP]
    totals = [int(amounts.sum()) for amounts in out]
    large = sum(1 for total in totals if total >= ROUND_STEP)
    target = min(int(round(round_share * large)), len(group))
    if group and target > 0:
        chosen = rng.choice(np.array(group), size=target, replace=False)
        for idx in sorted(int(i) for i in chosen):
            out[idx] = round_thousands(out[idx])
    return out


def _month_weights() -> list[tuple[date, float]]:
    """Workdays of every month with a higher weight near the period end."""
    out: list[tuple[date, float]] = []
    for period in PERIODS:
        days = workdays(date(int(period[:4]), int(period[5:]), 1), month_end(period))
        for idx, day in enumerate(days):
            weight = 1.0
            if idx >= len(days) - 3:
                weight = 4.0
            elif idx < 3:
                weight = 2.0
            out.append((day, weight))
    return out


def _prev_period(period: str) -> str | None:
    year, mon = (int(p) for p in period.split("-"))
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def build_ledger(bank: Bank, rng: np.random.Generator) -> Ledger:
    """Build the clean ledger: manual entries, automatic batches, fixed dates."""
    people = People.from_bank(bank)
    ledger = Ledger(people=people)

    pool = [
        emp
        for emp in sorted(people.personal)
        if people.org_unit[emp] == "Rechnungswesen"
        and people.job_function[emp] == "Sachbearbeitung"
        and people.eligible(emp)
    ]
    if len(pool) < 24:
        raise RuntimeError(f"only {len(pool)} eligible Rechnungswesen clerks")
    clerks = pool[-20:]

    analysts = [
        emp
        for emp in sorted(people.personal)
        if people.org_unit[emp] == "Rechnungswesen"
        and people.job_function[emp] == "Analyse"
        and people.eligible(emp)
        and emp not in clerks
    ]
    occasional = analysts[-5:]
    leads = [
        emp
        for emp in sorted(people.personal)
        if people.org_unit[emp] == "Rechnungswesen"
        and people.job_function[emp] == "Teamleitung"
        and people.eligible(emp)
    ]
    ledger.approvers = [people.personal[emp] for emp in leads]
    ledger.clerks = clerks
    ledger.occasional = occasional
    ledger.close_periods = tuple(CLOSE_BY_PERIOD)

    plans: list[tuple[str, float, int, float]] = []
    for idx, emp in enumerate(clerks):
        # near-miss preparer: 15 % round amounts, everybody else about 6 %
        ratio = 0.15 if idx == 1 else 0.06
        plans.append((emp, ratio, int(rng.integers(520, 561)), emp))
    plans.append((occasional[0], 0.06, 110, "small"))
    for emp in occasional[1:4]:
        plans.append((emp, 0.05, int(rng.integers(60, 130)), "occasional"))
    for emp in analysts[-8:-4]:
        plans.append((emp, 0.05, int(rng.integers(40, 90)), "occasional"))

    workdays_list = _month_weights()
    days = np.array([d.toordinal() for d, _w in workdays_list])
    weights = np.array([w for _d, w in workdays_list], dtype=float)
    weights /= weights.sum()

    entries: list[Entry] = []
    for emp, ratio, count, tag in plans:
        plan = [_draw_kind(rng) for _ in range(count)]
        if tag == "small":
            # 300 segments -> 600 line amounts (B-01 min_n near miss)
            while sum(_segment_count(kind) for kind in plan) > 300:
                plan.pop()
            while sum(_segment_count(kind) for kind in plan) < 300:
                plan.append(_draw_kind(rng))
        amounts = _assign_amounts(plan, rng, ratio)
        if tag == "small":
            # A preparer with about 600 line amounts whose first digits are
            # visibly off Benford only because the sample is small (B-01 min_n
            # near miss). Redrawn until the deviation is in that band.
            for _attempt in range(200):
                values = [int(a) for amounts_ in amounts for a in amounts_]
                if 0.016 <= mad_first(values) <= 0.03:
                    break
                amounts = _assign_amounts(plan, rng, ratio)
        picks = rng.choice(days, size=len(plan), p=weights)
        times = rng.integers(7 * 3600, 16 * 3600, size=len(plan))
        for i, kind in enumerate(plan):
            day = date.fromordinal(int(picks[i]))
            seconds = int(times[i])
            posting = day
            if day.day <= 4 and rng.random() < 0.32:
                prev = _prev_period(f"{day.year}-{day.month:02d}")
                if prev in CLOSE_BY_PERIOD:
                    posting = month_end(prev)
            label = _label(kind)
            if kind[0] == "single":
                debit, credit = kind[1][0], kind[1][1]
            else:
                debit, credit = "/".join(kind[1][0]), "/".join(kind[1][1])
            entries.append(
                Entry(
                    kind="manual",
                    entry_day=day,
                    entry_time=_clock(seconds),
                    posting_day=posting,
                    preparer=people.personal[emp],
                    segments=_segments_of(kind, amounts[i]),
                    description=f"{label} {debit}/{credit}",
                    reversible=_segment_count(kind) == 1 and (debit, credit) in REVERSIBLE,
                )
            )
    ledger.entries = entries
    _add_scheduled(ledger, people, clerks, occasional)
    _add_reversals(ledger, rng)
    _add_automatic(ledger, people, technical_accounts(bank), rng)
    return ledger


def _clock(seconds: int) -> str:
    seconds = max(0, min(seconds, 24 * 3600 - 1))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _label(kind: tuple) -> str:
    if kind[0] == "single":
        return "Sachkontobuchung"
    return "Umbuchung"


def _manual_entry(
    people: People,
    emp: str,
    day: date,
    seconds: int,
    posting: date,
    segments: list[Segment],
    description: str,
    **kwargs: object,
) -> Entry:
    entry = Entry(
        kind="manual",
        entry_day=day,
        entry_time=_clock(seconds),
        posting_day=posting,
        preparer=people.personal[emp],
        segments=segments,
        description=description,
        **kwargs,  # type: ignore[arg-type]
    )
    return entry


def _add_scheduled(
    ledger: Ledger, people: People, clerks: list[str], occasional: list[str]
) -> None:
    """Fixed manual entries of the near misses (spec section 4)."""
    entries = ledger.entries
    rent_account = people.personal[clerks[0]]
    # 1. monthly rent, same amount in every month (B-07 duplicate window).
    for month in range(1, 15):
        year = 2025 + (month - 1) // 12
        mon = (month - 1) % 12 + 1
        day = workdays(date(year, mon, 1), month_end(f"{year}-{mon:02d}"))[2]
        entries.append(
            _manual_entry(
                people,
                clerks[0],
                day,
                9 * 3600 + 17 * 60,
                day,
                [Segment("5200", "3400", 3_842_000)],
                "Miete Verwaltungsgebaeude",
                protected=True,
                frozen=True,
            )
        )
    # 2. manual entry one cent below the approval limit, no approver (B-08).
    day = date(2025, 5, 27)
    near_limit = _manual_entry(
        people,
        clerks[1],
        day,
        11 * 3600,
        day,
        [Segment("5700", "1100", MANUAL_LIMIT - 1)],
        "Sonstige Aufwendungen Einzelfall",
        protected=True,
        frozen=True,
    )
    near_limit.approval = "missing"
    entries.append(near_limit)
    # 3. bank working days 24 and 31 December (B-04).
    for day, seconds in ((date(2025, 12, 24), 9 * 3600), (date(2025, 12, 31), 14 * 3600)):
        entries.append(
            _manual_entry(
                people,
                clerks[2],
                day,
                seconds,
                day,
                [Segment("5310", "3400", 5_204_375 if day.day == 31 else 4_812_500)],
                "Jahresabschlussarbeiten EDV",
                protected=True,
                frozen=True,
            )
        )
    # 4. manual entry entered on the close date itself (B-05 near miss).
    for period in ("2025-03", "2025-06", "2025-09"):
        close = CLOSE_BY_PERIOD[period]
        entries.append(
            _manual_entry(
                people,
                clerks[3],
                close,
                (16 + int(period[5:]) % 2) * 3600 + 12 * 60,
                month_end(period),
                [Segment("9990", "1100", 2_345_710 + int(period[5:]) * 11)],
                f"Abschlussbuchung {period}",
                protected=True,
                frozen=True,
            )
        )
    # 5. accrual reversed on day 11 of the next period (B-05 near miss).
    original_day = date(2025, 10, 28)
    original = _manual_entry(
        people,
        clerks[4],
        original_day,
        15 * 3600,
        original_day,
        [Segment("2400", "5700", 6_248_310)],
        "Abgrenzung Raumkosten November",
        protected=True,
        frozen=True,
    )
    reversal_day = date(2025, 11, 11)
    reversal = _manual_entry(
        people,
        clerks[4],
        reversal_day,
        9 * 3600,
        reversal_day,
        [Segment("5700", "2400", 6_248_310)],
        "Storno Abgrenzung Raumkosten November",
        protected=True,
        frozen=True,
    )
    reversal.reversal_of = original
    entries.extend([original, reversal])
    # 6. reversal of an entry outside the period end (B-05 near miss).
    original_day = date(2025, 10, 24)
    original = _manual_entry(
        people,
        clerks[5],
        original_day,
        10 * 3600,
        original_day,
        [Segment("5700", "1100", 1_733_990)],
        "Korrektur Aufwand Oktober",
        protected=True,
        frozen=True,
    )
    reversal_day = date(2025, 11, 4)
    reversal = _manual_entry(
        people,
        clerks[5],
        reversal_day,
        10 * 3600,
        reversal_day,
        [Segment("1100", "5700", 1_733_990)],
        "Storno Korrektur Aufwand Oktober",
        protected=True,
        frozen=True,
    )
    reversal.reversal_of = original
    entries.extend([original, reversal])
    # The occasional preparers stay below the judgement thresholds; their
    # entries are part of the regular plan in ``build_ledger``.
    assert occasional and rent_account


def _add_reversals(ledger: Ledger, rng: np.random.Generator) -> None:
    """Ordinary mid-month corrections, reversed a few days later."""
    candidates = [
        e
        for e in ledger.entries
        if e.kind == "manual"
        and e.reversible
        and not e.protected
        and e.posting_day == e.entry_day
        and 8 <= e.entry_day.day <= 20
    ]
    if len(candidates) < 60:
        raise RuntimeError("not enough reversible manual entries")
    chosen = rng.choice(np.array(len(candidates)), size=40, replace=False)
    new: list[Entry] = []
    for idx in sorted(int(i) for i in chosen):
        original = candidates[idx]
        target = original.entry_day + timedelta(days=3 + int(rng.integers(0, 4)))
        while not is_workday(target) or target.month != original.entry_day.month:
            target += timedelta(days=1)
        segments = [Segment(s.credit, s.debit, s.amount) for s in original.segments]
        reversal = Entry(
            kind="manual",
            entry_day=target,
            entry_time=_clock(10 * 3600 + int(rng.integers(0, 3600))),
            posting_day=target,
            preparer=original.preparer,
            segments=segments,
            description=f"Storno {original.description}",
        )
        reversal.reversal_of = original
        original.frozen = True
        new.append(reversal)
    ledger.entries.extend(new)


def _add_automatic(
    ledger: Ledger, people: People, technical: list[str], rng: np.random.Generator
) -> None:
    """Nightly subledger batches (weekends included) plus fixed month-end runs."""
    if not technical:
        raise RuntimeError("no technical GL account in the bank")
    tech = technical[0]
    singles = [(d, c) for d, c, _w in AUTO_SINGLES]
    weights = np.array([w for _d, _c, w in AUTO_SINGLES], dtype=float)
    weights /= weights.sum()

    nights: list[date] = []
    counts: list[int] = []
    day = YEAR_START
    while day <= JOURNAL_AS_OF:
        base = 90 + int(rng.integers(0, 50))
        if is_weekend(day):
            base = int(base * 0.35)
        if day.day >= 26 or day.day <= 2:
            base = int(base * 1.8)
        nights.append(day)
        counts.append(base)
        day += timedelta(days=1)
    total = sum(counts)

    # Fee batches: one nightly entry with three segments, the counter side
    # going to the three clearing accounts in turn. They carry the fee income
    # account over the B-02 sample threshold.
    fee_total = len(nights) * FEE_SEGMENTS
    fee_pool = stratified_log_uniform(fee_total, rng)

    single_kind = rng.choice(np.arange(len(singles)), size=total, p=weights)
    pair_kind = rng.choice(np.arange(len(singles)), size=total, p=weights)
    three_kind = rng.choice(np.arange(len(singles)), size=total, p=weights)
    counts_three = rng.random(total) < 0.15
    counts_pair = rng.random(total) < 0.20
    pool = stratified_log_uniform(total * 5, rng)
    times = rng.integers(0, 5 * 3600, size=total)

    pos = 0
    auto: list[Entry] = []
    for idx, day in enumerate(nights):
        seconds = int(times[idx])
        day_entries: list[Entry] = [
            Entry(
                kind="auto",
                entry_day=day,
                entry_time=_clock(seconds),
                posting_day=day,
                preparer=tech,
                segments=[],
                description=f"Nachlauf {day.isoformat()}",
            )
        ]
        day_entries[0].segments = [
            Segment(FEE_POOL[k % len(FEE_POOL)], FEE_ACCOUNT, int(fee_pool[idx * FEE_SEGMENTS + k]))
            for k in range(FEE_SEGMENTS)
        ]
        for _ in range(counts[idx]):
            n = 3 if counts_three[pos] else (1 if not counts_pair[pos] else 2)
            segs: list[Segment] = []
            for k in range(n):
                if k == 0:
                    debit, credit = singles[single_kind[pos]]
                elif k == 1:
                    debit, credit = singles[pair_kind[pos]]
                else:
                    debit, credit = singles[three_kind[pos]]
                amount = int(pool[pos * 5 + k])
                segs.append(Segment(debit, credit, amount))
            day_entries.append(
                Entry(
                    kind="auto",
                    entry_day=day,
                    entry_time=_clock(seconds),
                    posting_day=day,
                    preparer=tech,
                    segments=segs,
                    description=f"Subledger-Lauf {day.isoformat()}",
                )
            )
            pos += 1
        auto.extend(day_entries)
    # Fixed monthly runs: payroll and depreciation.
    for month in range(1, 15):
        year = 2025 + (month - 1) // 12
        mon = (month - 1) % 12 + 1
        day = workdays(date(year, mon, 1), month_end(f"{year}-{mon:02d}"))[0]
        auto.append(
            Entry(
                kind="auto",
                entry_day=day,
                entry_time="04:00:00",
                posting_day=day,
                preparer=tech,
                segments=[
                    Segment("5100", "3710", 182_400_000),
                    Segment("5110", "3720", 38_250_000),
                    Segment("5120", "3810", 9_600_000),
                ],
                description="Lohnlauf",
            )
        )
        if year == 2025:
            auto.append(
                Entry(
                    kind="auto",
                    entry_day=day,
                    entry_time="04:30:00",
                    posting_day=day,
                    preparer=tech,
                    segments=[Segment("5500", "1990", 12_480_000)],
                    description="Abschreibungslauf",
                )
            )
    ledger.entries.extend(auto)


# ---------------------------------------------------------------------------
# settle the ledger: no duplicates, numbering, approvals
# ---------------------------------------------------------------------------


def resolve_duplicates(ledger: Ledger) -> None:
    """Remove accidental identical signatures within the seven-day window.

    Protected entries (the rent series, the deliberate duplicate cases) keep
    their amount; every other colliding entry gets its first amount bumped by
    one unit (a cent, or a whole thousand for a round entry), which keeps it
    a whole thousand when it was one.
    """
    manual = [e for e in ledger.entries if e.kind == "manual"]
    index: dict[tuple, list[tuple[date, Entry]]] = defaultdict(list)
    for entry in manual:
        if entry.protected:
            index[entry.signature()].append((entry.posting_day, entry))
    bumped = 0
    for entry in manual:
        if entry.protected:
            continue
        while True:
            key = entry.signature()
            recent = [
                day
                for day, _other in index.get(key, [])
                if abs((day - entry.posting_day).days) <= 7
            ]
            if not recent:
                index[key].append((entry.posting_day, entry))
                break
            step = ROUND_STEP if entry.segments[0].amount % ROUND_STEP == 0 else 1
            entry.segments[0].amount += step
            bumped += 1
            if bumped > 100_000:  # pragma: no cover - safety net
                raise RuntimeError("duplicate resolution does not terminate")


def number_entries(ledger: Ledger) -> None:
    """Assign ``number_range`` and ``doc_no`` per source and year."""
    counters: dict[str, int] = defaultdict(int)
    ordered = sorted(
        enumerate(ledger.entries),
        key=lambda pair: (
            pair[1].entry_day,
            pair[1].entry_time,
            pair[1].kind,
            pair[0],
        ),
    )
    for _idx, entry in ordered:
        prefix = "MJ" if entry.kind == "manual" else "AJ"
        number_range = f"{prefix}-{entry.entry_day.year}"
        counters[number_range] += 1
        entry.number_range = number_range
        entry.doc_no = f"{number_range}-{counters[number_range]:06d}"


def _approval_moment(entry: Entry, rng: np.random.Generator) -> datetime:
    posted = datetime.fromisoformat(entry.ts)
    base = posted
    if not is_workday(posted.date()) or posted.hour >= 14:
        base = datetime.combine(
            next_workday(posted.date() + timedelta(days=1)), datetime.min.time()
        )
        base = base.replace(hour=8)
    candidate = base + timedelta(minutes=int(rng.integers(5, 300)))
    if candidate.date() == posted.date() and candidate.hour * 60 + candidate.minute > 17 * 60 + 30:
        candidate = datetime.combine(
            next_workday(posted.date() + timedelta(days=1)), datetime.min.time()
        )
        candidate = candidate.replace(hour=9) + timedelta(minutes=int(rng.integers(0, 180)))
    if candidate < posted:
        candidate = posted + timedelta(minutes=5)
    if candidate.date() > JOURNAL_AS_OF:
        candidate = datetime.combine(posted.date(), datetime.min.time()).replace(hour=17, minute=0)
    return candidate


def _pick_approver(ledger: Ledger, entry: Entry, day: date, start: float) -> str:
    people = ledger.people
    pool = ledger.approvers
    preparer_emp = people.employee_of(entry.preparer)
    offset = int(start * len(pool)) % max(len(pool), 1)
    for step in range(len(pool)):
        account = pool[(offset + step) % len(pool)]
        employee = people.employee_of(account)
        if account == entry.preparer or employee == preparer_emp:
            continue
        if people.employed(employee, day):
            return account
    return ""


def assign_approvals(ledger: Ledger, rng: np.random.Generator, *, seed: int) -> None:
    """Four-eyes principle for all manual entries at or above the limit."""
    for entry in ledger.entries:
        entry.approver = ""
        entry.approved_ts = ""
        if entry.kind != "manual":
            continue
        if entry.approval == "missing":
            continue
        needed = entry.total >= MANUAL_LIMIT or entry.rnd < 0.5
        if entry.approval == "self":
            needed = True
        if not needed:
            continue
        if entry.approval == "self" and entry.total < MANUAL_LIMIT:
            # An approval the clean ledger does not have (X-01): its own stream, so
            # the timestamps drawn for every other entry stay as they were. The
            # stream carries the seed and the document number, so two seeds never
            # share a timestamp for the same document number.
            counter = int(entry.doc_no.rsplit("-", 1)[1])
            approved = _approval_moment(entry, np.random.default_rng([seed, 5, counter]))
        else:
            approved = _approval_moment(entry, rng)
        entry.approved_ts = approved.strftime("%Y-%m-%dT%H:%M:%S")
        if entry.approval == "self":
            entry.approver = entry.approver_account or entry.preparer
            continue
        if entry.approver_account:
            entry.approver = entry.approver_account
            continue
        approver = _pick_approver(ledger, entry, approved.date(), entry.rnd_pick)
        if approver:
            entry.approver = approver
        elif entry.total >= MANUAL_LIMIT:
            raise RuntimeError(f"no approver available for {entry.ts} {entry.total}")


def finalize_ledger(ledger: Ledger, rng: np.random.Generator, *, seed: int) -> None:
    resolve_duplicates(ledger)
    number_entries(ledger)
    assign_approvals(ledger, rng, seed=seed)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def build_analysis(ledger: Ledger) -> dict[str, object]:
    """Independent statistics used by the self check and the tests."""
    people = ledger.people
    manual = [e for e in ledger.entries if e.kind == "manual"]
    per_preparer: dict[str, list[int]] = defaultdict(list)
    per_account: dict[str, list[int]] = defaultdict(list)
    per_account_manual: dict[str, list[int]] = defaultdict(list)
    for entry in ledger.entries:
        for seg in entry.segments:
            amount = seg.amount
            per_account[seg.debit].append(amount)
            per_account[seg.credit].append(amount)
            if entry.kind == "manual":
                per_account_manual[seg.debit].append(amount)
                per_account_manual[seg.credit].append(amount)
                # every line is an amount for the preparer (debit or credit)
                per_preparer[people.employee_of(entry.preparer)].append(amount)
                per_preparer[people.employee_of(entry.preparer)].append(amount)
    rounds: dict[str, list[int]] = defaultdict(list)
    for entry in manual:
        if entry.total >= 100_000:
            rounds[people.employee_of(entry.preparer)].append(entry.total)
    pair_count: Counter[tuple[str, str]] = Counter()
    for entry in ledger.entries:
        seen = set()
        for seg in entry.segments:
            if (seg.debit, seg.credit) not in seen:
                seen.add((seg.debit, seg.credit))
                pair_count[(seg.debit, seg.credit)] += 1
    return {
        "per_preparer": per_preparer,
        "per_account": per_account,
        "per_account_manual": per_account_manual,
        "rounds": rounds,
        "pairs": pair_count,
        "manual": manual,
    }


def numbers(ledger: Ledger) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for entry in ledger.entries:
        if entry.doc_no:
            out[entry.number_range].append(int(entry.doc_no.rsplit("-", 1)[1]))
    return out


def check_clean_ledger(ledger: Ledger) -> list[str]:
    """Return the problems of a supposedly clean ledger (empty when clean)."""
    problems: list[str] = []
    people = ledger.people
    stats = build_analysis(ledger)
    for employee, amounts in sorted(stats["per_preparer"].items()):  # type: ignore[union-attr]
        if len(amounts) >= BENFORD_FIRST_MIN_N and mad_first(amounts) > 0.013:
            problems.append(f"B-01 would fire for {employee}: MAD {mad_first(amounts):.4f}")
    for account, amounts in sorted(stats["per_account"].items()):  # type: ignore[union-attr]
        if len(amounts) >= BENFORD_TWO_MIN_N and mad_first_two(amounts) > 0.0018:
            problems.append(
                f"B-02 would fire for {account}: MAD {mad_first_two(amounts):.4f} n={len(amounts)}"
            )
    for account, amounts in sorted(stats["per_account_manual"].items()):  # type: ignore[union-attr]
        if len(amounts) >= BENFORD_TWO_MIN_N and mad_first_two(amounts) > 0.0018:
            problems.append(
                f"B-02 (manual lines) would fire for {account}: MAD "
                f"{mad_first_two(amounts):.4f} n={len(amounts)}"
            )
    for employee, totals in sorted(stats["rounds"].items()):  # type: ignore[union-attr]
        share = sum(1 for t in totals if t % ROUND_STEP == 0) / len(totals)
        if len(totals) >= 30 and share > 0.20:
            problems.append(f"B-03 would fire for {employee}: round share {share:.3f}")
    signatures: dict[tuple, list[Entry]] = defaultdict(list)
    for entry in ledger.entries:
        if entry.kind != "manual":
            continue
        signatures[entry.signature()].append(entry)
        if not is_workday(entry.entry_day):
            problems.append(f"B-04 off-hours manual entry {entry.entry_day} {entry.doc_no}")
        close = CLOSE_BY_PERIOD.get(f"{entry.posting_day.year}-{entry.posting_day.month:02d}")
        if close is not None and entry.entry_day > close:
            problems.append(f"B-05 post-closing entry {entry.doc_no}")
        if entry.total >= MANUAL_LIMIT:
            if not entry.approver:
                problems.append(f"B-08 unapproved entry {entry.doc_no}")
            elif people.employee_of(entry.approver) == people.employee_of(entry.preparer):
                problems.append(f"B-08 self approval {entry.doc_no}")
        if not people.employed(people.employee_of(entry.preparer), entry.entry_day):
            problems.append(f"preparer not employed {entry.doc_no}")
        if entry.total <= 0 or any(s.amount <= 0 for s in entry.segments):
            problems.append(f"non-positive amount {entry.doc_no}")
        if people.org_unit.get(people.employee_of(entry.preparer)) != "Rechnungswesen":
            problems.append(f"B-10 unusual unit {entry.doc_no}")
        if people.job_function.get(people.employee_of(entry.preparer)) == "Administration":
            problems.append(f"B-10 administration {entry.doc_no}")
    for signature, group in signatures.items():
        days = sorted(e.posting_day for e in group)
        for left, right in zip(days, days[1:], strict=False):
            if (right - left).days <= 7:
                problems.append(f"B-07 duplicate signature {signature[:1]} {left} {right}")
    pairs: Counter = stats["pairs"]  # type: ignore[assignment]
    for (debit, credit), count in sorted(pairs.items()):
        if count < 3 and (
            ACCOUNT_CLASS[debit] in {"income", "equity", "suspense"}
            or ACCOUNT_CLASS[credit] in {"income", "equity", "suspense"}
        ):
            problems.append(f"B-09 rare pair {debit}/{credit}: {count}")
    for number_range, values in sorted(numbers(ledger).items()):
        if sorted(values) != list(range(1, len(values) + 1)):
            problems.append(f"B-06 gap or duplicate in {number_range}")
    return problems


def apply_completeness(ledger: Ledger) -> list[dict[str, str]]:
    """Remove the planned gaps and write the duplicate header (after numbering).

    Both happen once the document numbers are assigned, so the number range
    has a hole (or a repeated ``doc_no``) exactly at the planned place.
    """
    issues: list[dict[str, str]] = []
    for run in ledger.gap_runs:
        values = sorted(int(e.doc_no.rsplit("-", 1)[1]) for e in run)
        first, last = values[0], values[-1]
        number_range = run[0].number_range
        entities = [f"{number_range}:gap:{first:06d}-{last:06d}"]
        if len(values) == 1:
            entities = [f"{number_range}:gap:{first:06d}-{first:06d}"]
        issues.append(
            {
                "rule_id": "B-06",
                "entity": entities[0],
                "note": (
                    f"Document numbers {first:06d} to {last:06d} are missing from {number_range}"
                    if len(values) > 1
                    else f"Document number {first:06d} is missing from {number_range}"
                ),
            }
        )
        for entry in run:
            ledger.entries.remove(entry)
            ledger.removed.append(entry)
    if ledger.duplicate is not None:
        original = ledger.duplicate
        clone = Entry(
            kind=original.kind,
            entry_day=original.entry_day,
            entry_time=original.entry_time,
            posting_day=original.posting_day,
            preparer=original.preparer,
            segments=list(original.segments),
            description=original.description,
            reversal_of=original.reversal_of,
            approver=original.approver,
            approved_ts=original.approved_ts,
            doc_no=original.doc_no,
            number_range=original.number_range,
            frozen=True,
        )
        ledger.entries.append(clone)
        issues.append(
            {
                "rule_id": "B-06",
                "entity": f"{original.number_range}:dup:{original.doc_no}",
                "note": (
                    f"Document number {original.doc_no} is used by two entry headers "
                    f"(second header at {original.ts})"
                ),
            }
        )
    return issues


def last_workday_before(day: date) -> date:
    """The last workday strictly before *day*."""
    day -= timedelta(days=1)
    while not is_workday(day):
        day -= timedelta(days=1)
    return day


def first_workdays(day: date, count: int) -> list[date]:
    """The first *count* workdays strictly after *day*."""
    out: list[date] = []
    cursor = day
    while len(out) < count:
        cursor += timedelta(days=1)
        if is_workday(cursor):
            out.append(cursor)
    return out


def previous_period(day: date) -> str:
    return _prev_period(f"{day.year}-{day.month:02d}")  # type: ignore[return-value]


def cross_account_employee(bank: Bank, people: People | None = None) -> str:
    """An employee who holds two personal GL accounts (module A near miss 7).

    The clean bank has none; module A's SOD injection creates exactly one
    employee with two GL accounts. That employee is reused for the
    self-approval cases of B-08: a second account does not make a second pair
    of eyes.
    """
    people = people or People.from_bank(bank)
    by_employee: dict[str, list[str]] = defaultdict(list)
    for account, employee in people.accounts.items():
        by_employee[employee].append(account)
    candidates = sorted(
        employee
        for employee, accounts in by_employee.items()
        if len(accounts) > 1
        and people.org_unit.get(employee) == "Rechnungswesen"
        and people.eligible(employee)
    )
    if not candidates:
        raise RuntimeError("no employee with two GL accounts for the B-08 cases")
    return candidates[0]


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

GL_COLUMNS = ["account_no", "name", "account_class", "manual_posting"]
JOURNAL_ENTRY_COLUMNS = [
    "doc_no",
    "number_range",
    "source",
    "posting_date",
    "entry_ts",
    "preparer_account_id",
    "approver_account_id",
    "approved_ts",
    "description",
    "reversal_of",
]
JOURNAL_LINE_COLUMNS = ["doc_no", "line_no", "account_no", "debit", "credit"]
CLOSE_COLUMNS = ["period", "close_date"]
LIMIT_COLUMNS = ["source", "limit"]


def write_journal(ledger: Ledger, out_dir) -> None:  # noqa: ANN001
    """Write the five module B files (UTF-8, sorted, no floats)."""
    import csv
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def write(name: str, columns: list[str], rows: list[dict[str, str]]) -> None:
        with (out / name).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    write(
        "gl_accounts.csv",
        GL_COLUMNS,
        [
            {
                "account_no": no,
                "name": name,
                "account_class": cls,
                "manual_posting": "true" if manual else "false",
            }
            for no, name, cls, manual in sorted(CHART_OF_ACCOUNTS)
        ],
    )
    write(
        "close_calendar.csv",
        CLOSE_COLUMNS,
        [{"period": period, "close_date": day.isoformat()} for period, day in CLOSE_CALENDAR],
    )
    write(
        "approval_limits.csv",
        LIMIT_COLUMNS,
        [
            {"source": "manual", "limit": cents_text(MANUAL_LIMIT)},
            {"source": "automatic", "limit": cents_text(AUTO_LIMIT)},
        ],
    )
    entries = sorted(
        ledger.entries,
        key=lambda e: (e.doc_no, e.ts, e.preparer, e.reversal_of, e.description),
    )
    write(
        "journal_entries.csv",
        JOURNAL_ENTRY_COLUMNS,
        [
            {
                "doc_no": e.doc_no,
                "number_range": e.number_range,
                "source": "manual" if e.kind == "manual" else "automatic",
                "posting_date": e.posting_day.isoformat(),
                "entry_ts": e.ts,
                "preparer_account_id": e.preparer,
                "approver_account_id": e.approver,
                "approved_ts": e.approved_ts,
                "description": e.description,
                "reversal_of": e.reversal_of.doc_no if e.reversal_of else "",
            }
            for e in entries
        ],
    )
    lines: list[dict[str, str]] = []
    for entry in entries:
        for number, seg in enumerate(entry.segments, start=1):
            lines.append(
                {
                    "doc_no": entry.doc_no,
                    "line_no": str(number),
                    "account_no": seg.debit,
                    "debit": cents_text(seg.amount),
                    "credit": "",
                }
            )
            lines.append(
                {
                    "doc_no": entry.doc_no,
                    "line_no": str(number),
                    "account_no": seg.credit,
                    "debit": "",
                    "credit": cents_text(seg.amount),
                }
            )
    lines.sort(
        key=lambda row: (row["doc_no"], int(row["line_no"]), row["account_no"], row["debit"])
    )
    write("journal_lines.csv", JOURNAL_LINE_COLUMNS, lines)


def plan_completeness(ledger: Ledger, rng: np.random.Generator) -> None:
    """Choose the B-06 gaps and the duplicated header after numbering.

    Only entries no other injection touches are removed, and the gaps sit in
    the interior of a number range so nothing else moves.
    """
    groups: dict[str, list[Entry]] = defaultdict(list)
    for entry in ledger.entries:
        if entry.doc_no:
            groups[entry.number_range].append(entry)

    def runs(entries: list[Entry]) -> list[list[Entry]]:
        ordered = sorted(entries, key=lambda e: int(e.doc_no.rsplit("-", 1)[1]))
        out: list[list[Entry]] = []
        current: list[Entry] = []
        for entry in ordered:
            counter = int(entry.doc_no.rsplit("-", 1)[1])
            if current and counter != int(current[-1].doc_no.rsplit("-", 1)[1]) + 1:
                out.append(current)
                current = []
            current.append(entry)
        if current:
            out.append(current)
        return out

    def bounds(number_range: str) -> tuple[int, int]:
        values = [int(e.doc_no.rsplit("-", 1)[1]) for e in groups.get(number_range, [])]
        return min(values), max(values)

    def counter_of(entry: Entry) -> int:
        return int(entry.doc_no.rsplit("-", 1)[1])

    def windows(entries: list[Entry], number_range: str, size: int) -> list[list[Entry]]:
        """Interior windows of *size* consecutive numbers (never an end)."""
        low, high = bounds(number_range)
        out: list[list[Entry]] = []
        for run in runs(entries):
            for start in range(len(run) - size + 1):
                window = run[start : start + size]
                if counter_of(window[0]) > low and counter_of(window[-1]) < high:
                    out.append(window)
        return out

    manual = [e for e in groups.get("MJ-2025", []) if not e.frozen]
    automatic = [e for e in groups.get("AJ-2025", []) if not e.frozen]
    three = windows(manual, "MJ-2025", 3)
    two = windows(manual, "MJ-2025", 2)
    one = windows(automatic, "AJ-2025", 1)
    if not three or not two or not one:
        raise RuntimeError("not enough interior windows for the B-06 gaps")
    three.sort(key=lambda w: counter_of(w[0]))
    two.sort(key=lambda w: counter_of(w[0]))
    first = three[0]
    second = next(w for w in two if counter_of(w[0]) > counter_of(first[-1]) + 200)
    ledger.gap_runs = [first, second, one[len(one) // 2]]
    low, high = bounds("AJ-2025")
    duplicates = [e for e in automatic if len(e.segments) >= 2 and low < counter_of(e) < high]
    if not duplicates:
        raise RuntimeError("no automatic entry available for the B-06 duplicate")
    ledger.duplicate = duplicates[len(duplicates) // 2]
    assert rng is not None
