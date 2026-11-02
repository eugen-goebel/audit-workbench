"""Tests for the synthetic journal ledger of module B.

The detection rules of ``audit_workbench.journal`` are implemented by a
parallel workstream. These tests therefore compute every rule from the CSV
files again, straight from the semantics of ``docs/design/module-b-journal.md``
and independently of the production code, and compare the result with
``ground_truth.json``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import holidays
import pytest

from audit_workbench.synth import generate_bank

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
JOURNAL_AS_OF = date(2026, 2, 28)
HE = holidays.Germany(subdiv="HE", years=[2025, 2026])

MODULE_A_FILES = [
    "account_roles.csv",
    "accounts.csv",
    "entitlements.csv",
    "hr_employees.csv",
    "recert_campaigns.csv",
    "recert_decisions.csv",
    "role_entitlements.csv",
    "role_hierarchy.csv",
    "roles.csv",
]

#: hashes of the module A files of the generator before the journal was added.
#: ``accounts.csv`` and ``account_roles.csv`` carry the phase 4 changes (spec section 6):
#: valid_from / created_at of the two GL SoD employees, the first-day assignment of one
#: clerk and the last_login of the two developer leavers. The other files are unchanged.
MODULE_A_HASHES = {
    7: {
        "account_roles.csv": "b8145f6906626ed3010bc721d6b371b88604a7e5a1ad434d62a80ad83a6cc07c",
        "accounts.csv": "235a67e77645db57e8eff1a623534d0d7ba62c9c225da4e3b90b486669b07058",
        "entitlements.csv": "044dcc17d56e5f54625bc7344074ff050d08a38de691799de12b68852d9f1240",
        "hr_employees.csv": "e7fc77c235f48bc066b6c064c141740f484b1212e41764e3a5ddf41ad2454767",
        "recert_campaigns.csv": "c6542aaff615ed19c3340e36932eb8edfc23512a4a1adf1bfd9b1fac094c31b1",
        "recert_decisions.csv": "53814cae0d78adf417a1905f94289b2f4fc71caba88ea1d64e49328593c97db8",
        "role_entitlements.csv": "cc6007873d100615b325277526da90428b7767da02f71938445f1d27c0043357",
        "role_hierarchy.csv": "862f28f13793aff1ca3cc302441772c5bac8faab65e885e6a6f5a4306ed68ce3",
        "roles.csv": "bfeffd47bba75d526af3a1aec197cf9c8e4d15340dff227713a64914629d29af",
    },
    11: {
        "account_roles.csv": "7d613408f904b246dd45c08061a2e2980d2da4cd120839380175fbfd44c6f270",
        "accounts.csv": "eeed4815a97a5d259608b2d82b240f9659c8687ff466757744c2335711aa6a2e",
        "entitlements.csv": "044dcc17d56e5f54625bc7344074ff050d08a38de691799de12b68852d9f1240",
        "hr_employees.csv": "a3ca91017e8dd13a2ee567e268ba6d7f8b5facb333c40bdbecd1103f8135219f",
        "recert_campaigns.csv": "c6542aaff615ed19c3340e36932eb8edfc23512a4a1adf1bfd9b1fac094c31b1",
        "recert_decisions.csv": "671b0a167f53e34424563fcdb715a6efce9ae953eb6ce0e0cad73798aa64bf1f",
        "role_entitlements.csv": "cc6007873d100615b325277526da90428b7767da02f71938445f1d27c0043357",
        "role_hierarchy.csv": "862f28f13793aff1ca3cc302441772c5bac8faab65e885e6a6f5a4306ed68ce3",
        "roles.csv": "bfeffd47bba75d526af3a1aec197cf9c8e4d15340dff227713a64914629d29af",
    },
}


# ---------------------------------------------------------------------------
# loading and small numeric helpers
# ---------------------------------------------------------------------------


def read_csv(directory: Path, name: str) -> list[dict[str, str]]:
    with (directory / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cents(text: str) -> int:
    """``'1234.50'`` -> ``123450`` without any float."""
    if not text:
        return 0
    whole, _, fraction = text.partition(".")
    return int(whole) * 100 + int((fraction + "00")[:2])


def amount_of(entry_id: str, lines: list[dict[str, str]]) -> int:
    return sum(cents(line["debit"]) for line in lines if line["doc_no"] == entry_id)


def first_digits(values: list[int], digits: int) -> Counter:
    out: Counter = Counter()
    for value in values:
        text = str(value).lstrip("0")
        out[int(text[:digits])] += 1
    return out


def mad_first(values: list[int]) -> float:
    values = [v for v in values if v >= 1000]
    if not values:
        return 9.0
    observed = first_digits(values, 1)
    return sum(abs(observed[d] / len(values) - benford_expected(d)) for d in range(1, 10)) / 9


def benford_expected(digit: int) -> float:
    import math

    return math.log10(1 + 1 / digit)


def mad_first_two(values: list[int]) -> float:
    import math

    values = [v for v in values if v >= 1000]
    if not values:
        return 9.0
    observed = first_digits(values, 2)
    return sum(abs(observed[d] / len(values) - math.log10(1 + 1 / d)) for d in range(10, 100)) / 90


class Book:
    """The five module B files plus the lookup tables of module A."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.gl = {row["account_no"]: row for row in read_csv(directory, "gl_accounts.csv")}
        self.entries = read_csv(directory, "journal_entries.csv")
        self.lines = read_csv(directory, "journal_lines.csv")
        self.close = {
            row["period"]: date.fromisoformat(row["close_date"])
            for row in read_csv(directory, "close_calendar.csv")
        }
        self.limits = {
            row["source"]: cents(row["limit"]) for row in read_csv(directory, "approval_limits.csv")
        }
        self.employees = {
            row["employee_id"]: row for row in read_csv(directory, "hr_employees.csv")
        }
        self.account_owner = {
            row["account_id"]: row["employee_id"]
            for row in read_csv(directory, "accounts.csv")
            if row["employee_id"]
        }
        self.by_doc: dict[str, list[dict[str, str]]] = defaultdict(list)
        for entry in self.entries:
            self.by_doc[entry["doc_no"]].append(entry)
        self.lines_by_doc: dict[str, list[dict[str, str]]] = defaultdict(list)
        for line in self.lines:
            self.lines_by_doc[line["doc_no"]].append(line)
        self.totals = {
            doc: sum(cents(line["debit"]) for line in rows)
            for doc, rows in self.lines_by_doc.items()
        }

    # -- helpers ---------------------------------------------------------
    def header(self, doc_no: str) -> dict[str, str]:
        return self.by_doc[doc_no][0]

    def employee(self, account: str) -> str:
        return self.account_owner.get(account, "")

    def manual(self) -> list[dict[str, str]]:
        return [e for e in self.entries if e["source"] == "manual"]

    def signature(self, doc_no: str) -> tuple:
        return tuple(
            sorted(
                (line["account_no"], cents(line["debit"]), cents(line["credit"]))
                for line in self.lines_by_doc[doc_no]
            )
        )


class Findings:
    """The re-implemented rules of spec section 3."""

    def __init__(self, book: Book) -> None:
        self.book = book
        self.found: dict[str, set[str]] = defaultdict(set)

    def add(self, rule: str, entity: str) -> None:
        self.found[rule].add(entity)

    def run(self) -> dict[str, set[str]]:
        self.b01()
        self.b02()
        self.b03()
        self.b04()
        self.b05()
        self.b06()
        self.b07()
        self.b08()
        self.b09()
        self.b10()
        return self.found

    # -- B-01 ------------------------------------------------------------
    def b01(self) -> None:
        book = self.book
        groups: dict[str, list[int]] = defaultdict(list)
        manual_docs = {e["doc_no"] for e in book.manual()}
        for line in book.lines:
            if line["doc_no"] not in manual_docs:
                continue
            value = cents(line["debit"]) or cents(line["credit"])
            groups[book.employee(book.header(line["doc_no"])["preparer_account_id"])].append(value)
        for employee, values in groups.items():
            if len(values) >= 1000 and mad_first(values) > 0.015:
                self.add("B-01", employee)

    # -- B-02 ------------------------------------------------------------
    def b02(self) -> None:
        groups: dict[str, list[int]] = defaultdict(list)
        for line in self.book.lines:
            groups[line["account_no"]].append(cents(line["debit"]) or cents(line["credit"]))
        for account, values in groups.items():
            if len(values) >= 5000 and mad_first_two(values) > 0.0022:
                self.add("B-02", account)

    # -- B-03 ------------------------------------------------------------
    def b03(self) -> None:
        groups: dict[str, list[int]] = defaultdict(list)
        for entry in self.book.manual():
            total = self.book.totals.get(entry["doc_no"], 0)
            if total >= 100_000:
                groups[self.book.employee(entry["preparer_account_id"])].append(total)
        for employee, totals in groups.items():
            if len(totals) >= 30:
                share = sum(1 for total in totals if total % 100_000 == 0) / len(totals)
                if share > 0.25:
                    self.add("B-03", employee)

    # -- B-04 ------------------------------------------------------------
    def b04(self) -> None:
        for entry in self.book.manual():
            day = datetime.fromisoformat(entry["entry_ts"]).date()
            if day.weekday() >= 5 or day in HE:
                self.add("B-04", entry["doc_no"])

    # -- B-05 ------------------------------------------------------------
    def b05(self) -> None:
        book = self.book
        reversals = defaultdict(list)
        for entry in book.entries:
            if entry["reversal_of"]:
                reversals[entry["reversal_of"]].append(entry)
        for entry in book.entries:
            posting = date.fromisoformat(entry["posting_date"])
            period = posting.strftime("%Y-%m")
            close = book.close.get(period)
            entered = datetime.fromisoformat(entry["entry_ts"]).date()
            if close is not None and entered > close:
                self.add("B-05", entry["doc_no"])
                continue
            days_in_month = (posting.replace(day=28) + timedelta(days=4)).replace(
                day=1
            ) - timedelta(days=1)
            at_end = (days_in_month - posting).days < 5
            if at_end:
                next_first = days_in_month + timedelta(days=1)
                for reversal in reversals.get(entry["doc_no"], []):
                    reversed_on = date.fromisoformat(reversal["posting_date"])
                    if (
                        reversed_on.year == next_first.year
                        and reversed_on.month == next_first.month
                    ):
                        if reversed_on.day <= 10:
                            self.add("B-05", entry["doc_no"])

    # -- B-06 ------------------------------------------------------------
    def b06(self) -> None:
        for entry in self.book.entries:
            if len(self.book.by_doc[entry["doc_no"]]) > 1:
                self.add("B-06", f"{entry['number_range']}:dup:{entry['doc_no']}")
        ranges: dict[str, list[int]] = defaultdict(list)
        for entry in self.book.entries:
            ranges[entry["number_range"]].append(int(entry["doc_no"].rsplit("-", 1)[1]))
        for number_range, values in ranges.items():
            present = set(values)
            missing = [v for v in range(min(values), max(values) + 1) if v not in present]
            run: list[int] = []
            for value in missing + [None]:
                if run and (value is None or value != run[-1] + 1):
                    self.add("B-06", f"{number_range}:gap:{run[0]:06d}-{run[-1]:06d}")
                    run = []
                if value is not None:
                    run.append(value)

    # -- B-07 ------------------------------------------------------------
    def b07(self) -> None:
        book = self.book
        groups: dict[tuple, list[dict[str, str]]] = defaultdict(list)
        for entry in book.manual():
            groups[book.signature(entry["doc_no"])].append(entry)
        for entries in groups.values():
            entries.sort(key=lambda e: (e["entry_ts"], e["doc_no"]))
            for earlier, later in zip(entries, entries[1:], strict=False):
                if (
                    earlier["reversal_of"] == later["doc_no"]
                    or later["reversal_of"] == earlier["doc_no"]
                ):
                    continue
                left = date.fromisoformat(earlier["posting_date"])
                right = date.fromisoformat(later["posting_date"])
                if abs((right - left).days) <= 7:
                    self.add("B-07", later["doc_no"])

    # -- B-08 ------------------------------------------------------------
    def b08(self) -> None:
        limit = self.book.limits["manual"]
        for entry in self.book.manual():
            if self.book.totals.get(entry["doc_no"], 0) < limit:
                continue
            approver = entry["approver_account_id"]
            if not approver or self.book.employee(approver) == self.book.employee(
                entry["preparer_account_id"]
            ):
                self.add("B-08", entry["doc_no"])

    # -- B-09 ------------------------------------------------------------
    def b09(self) -> None:
        book = self.book
        pairs: Counter = Counter()
        for entry in book.entries:
            seen = set()
            debits = [
                line["account_no"]
                for line in book.lines_by_doc[entry["doc_no"]]
                if cents(line["debit"])
            ]
            credits = [
                line["account_no"]
                for line in book.lines_by_doc[entry["doc_no"]]
                if cents(line["credit"])
            ]
            for debit in debits:
                for credit in credits:
                    if (debit, credit) not in seen:
                        seen.add((debit, credit))
                        pairs[(debit, credit)] += 1
        for entry in book.manual():
            debits = [
                line["account_no"]
                for line in book.lines_by_doc[entry["doc_no"]]
                if cents(line["debit"])
            ]
            credits = [
                line["account_no"]
                for line in book.lines_by_doc[entry["doc_no"]]
                if cents(line["credit"])
            ]
            for debit in debits:
                for credit in credits:
                    classes = {book.gl[debit]["account_class"], book.gl[credit]["account_class"]}
                    if pairs[(debit, credit)] < 3 and classes & {"income", "equity", "suspense"}:
                        self.add("B-09", entry["doc_no"])

    # -- B-10 ------------------------------------------------------------
    def b10(self) -> None:
        for entry in self.book.manual():
            employee = self.book.employees.get(self.book.employee(entry["preparer_account_id"]), {})
            if (
                employee.get("org_unit") != "Rechnungswesen"
                or employee.get("job_function") == "Administration"
            ):
                self.add("B-10", entry["doc_no"])


# ---------------------------------------------------------------------------
# fixtures: one generation per seed
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def clean_dir(tmp_path_factory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("journal_clean"), seed=7, inject=False)


@pytest.fixture(scope="session")
def injected_dir(tmp_path_factory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("journal_injected"), seed=7)


@pytest.fixture(scope="session")
def seed11_dir(tmp_path_factory) -> Path:
    return generate_bank(tmp_path_factory.mktemp("journal_seed11"), seed=11)


@pytest.fixture(scope="session")
def clean_book(clean_dir: Path) -> Book:
    return Book(clean_dir)


@pytest.fixture(scope="session")
def book(injected_dir: Path) -> Book:
    return Book(injected_dir)


@pytest.fixture(scope="session")
def truth(injected_dir: Path) -> dict:
    return json.loads((injected_dir / "ground_truth.json").read_text())


# ---------------------------------------------------------------------------
# files and schema
# ---------------------------------------------------------------------------


def test_files_and_header(clean_dir: Path) -> None:
    expected = {
        "gl_accounts.csv": ["account_no", "name", "account_class", "manual_posting"],
        "journal_entries.csv": [
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
        ],
        "journal_lines.csv": ["doc_no", "line_no", "account_no", "debit", "credit"],
        "close_calendar.csv": ["period", "close_date"],
        "approval_limits.csv": ["source", "limit"],
    }
    for name, columns in expected.items():
        with (clean_dir / name).open(encoding="utf-8") as fh:
            assert fh.readline().strip().split(",") == columns, name


def test_ground_truth_keys_unchanged(truth: dict) -> None:
    assert truth["as_of"] == "2026-09-30"
    assert truth["seed"] == 7
    assert truth["journal_as_of"] == "2026-02-28"
    assert any(issue["rule_id"].startswith("A-") for issue in truth["issues"])


# ---------------------------------------------------------------------------
# clean ledger: no B finding at all
# ---------------------------------------------------------------------------


def test_clean_ledger_has_no_findings(clean_book: Book) -> None:
    findings = Findings(clean_book).run()
    assert {rule: sorted(entities) for rule, entities in findings.items() if entities} == {}


def test_clean_ground_truth_has_no_journal_issues(clean_dir: Path) -> None:
    truth = json.loads((clean_dir / "ground_truth.json").read_text())
    assert [i for i in truth["issues"] if i["rule_id"].startswith("B-")] == []


# ---------------------------------------------------------------------------
# injected ledger: exactly the ground truth
# ---------------------------------------------------------------------------


def test_injected_ledger_matches_ground_truth(book: Book, truth: dict) -> None:
    found = Findings(book).run()
    want = defaultdict(set)
    for issue in truth["issues"]:
        if issue["rule_id"].startswith("B-"):
            want[issue["rule_id"]].add(issue["entity"])
    for rule in SPEC_COUNTS:
        assert sorted(found.get(rule, set())) == sorted(want[rule]), rule


def test_injected_counts_match_spec(truth: dict) -> None:
    counts = Counter(i["rule_id"] for i in truth["issues"] if i["rule_id"].startswith("B-"))
    assert dict(counts) == SPEC_COUNTS
    assert sum(counts.values()) == 42


def test_ground_truth_entities_are_known(book: Book, truth: dict) -> None:
    for issue in truth["issues"]:
        if not issue["rule_id"].startswith("B-"):
            continue
        assert issue["note"]
        if issue["rule_id"] in {"B-01", "B-02", "B-03"}:
            continue
        if issue["rule_id"] == "B-06":
            assert ":gap:" in issue["entity"] or ":dup:" in issue["entity"]
            continue
        assert issue["entity"] in book.by_doc, issue


def test_seed_11_is_exact_too(seed11_dir: Path) -> None:
    book = Book(seed11_dir)
    truth = json.loads((seed11_dir / "ground_truth.json").read_text())
    found = Findings(book).run()
    want = defaultdict(set)
    for issue in truth["issues"]:
        if issue["rule_id"].startswith("B-"):
            want[issue["rule_id"]].add(issue["entity"])
    for rule in SPEC_COUNTS:
        assert sorted(found.get(rule, set())) == sorted(want[rule]), rule


# ---------------------------------------------------------------------------
# volume and clean-data properties (spec section 4)
# ---------------------------------------------------------------------------


def test_volume(book: Book) -> None:
    assert 55_000 <= len(book.entries) <= 65_000
    assert 170_000 <= len(book.lines) <= 190_000
    manual = len(book.manual())
    assert 0.15 <= manual / len(book.entries) <= 0.25


def test_clerk_volume(book: Book) -> None:
    line_counts: dict[str, list[int]] = defaultdict(list)
    manual_docs = {e["doc_no"] for e in book.manual()}
    for line in book.lines:
        if line["doc_no"] in manual_docs:
            employee = book.employee(book.header(line["doc_no"])["preparer_account_id"])
            line_counts[employee].append(1)
    clerks = [len(values) for values in line_counts.values() if len(values) >= 1000]
    occasional = [len(values) for values in line_counts.values() if len(values) < 1000]
    assert len(clerks) >= 20
    assert min(clerks) > 1000
    assert occasional and max(occasional) < 1000


def test_entries_are_balanced_and_positive(book: Book) -> None:
    for doc_no, lines in book.lines_by_doc.items():
        debit = sum(cents(line["debit"]) for line in lines)
        credit = sum(cents(line["credit"]) for line in lines)
        assert debit == credit > 0, doc_no
        for line in lines:
            assert bool(line["debit"]) != bool(line["credit"]), (doc_no, line)
            assert cents(line["debit"]) + cents(line["credit"]) >= 1000, (doc_no, line)


def test_all_amounts_are_integer_cents(book: Book) -> None:
    for line in book.lines:
        for value in (line["debit"], line["credit"]):
            if value:
                assert value.split(".")[1] and len(value.split(".")[1]) == 2, value


def test_entries_are_inside_the_extract(book: Book) -> None:
    for entry in book.entries:
        timestamp = datetime.fromisoformat(entry["entry_ts"])
        assert timestamp.date() <= JOURNAL_AS_OF
        assert datetime.fromisoformat(entry["posting_date"]).date() <= JOURNAL_AS_OF
        if entry["approved_ts"]:
            approved = datetime.fromisoformat(entry["approved_ts"])
            assert approved >= timestamp and approved.date() <= JOURNAL_AS_OF, entry["doc_no"]


def test_preparer_and_approver_are_employed(clean_book: Book) -> None:
    def employed(employee: str, moment: datetime) -> bool:
        row = book.employees[employee]
        if moment.date() < date.fromisoformat(row["entry_date"]):
            return False
        exit_date = row["exit_date"]
        return not exit_date or date.fromisoformat(exit_date) > moment.date()

    book = clean_book
    for entry in book.entries:
        preparer = book.employee(entry["preparer_account_id"])
        if not preparer:  # automatic entries: technical account without owner
            continue
        assert employed(preparer, datetime.fromisoformat(entry["entry_ts"])), entry["doc_no"]
        approver = entry["approver_account_id"]
        if approver:
            assert employed(book.employee(approver), datetime.fromisoformat(entry["approved_ts"]))
            assert book.employee(approver) != preparer, entry["doc_no"]


def test_number_ranges(book: Book) -> None:
    ranges = {entry["number_range"] for entry in book.entries}
    assert {"MJ-2025", "AJ-2025", "MJ-2026", "AJ-2026"} <= ranges
    for entry in book.entries:
        assert entry["doc_no"].startswith(entry["number_range"] + "-")
        assert (
            entry["doc_no"]
            == f"{entry['number_range']}-{int(entry['doc_no'].rsplit('-', 1)[1]):06d}"
        )


def test_close_calendar_and_limits(book: Book) -> None:
    assert sorted(book.close) == [f"2025-{month:02d}" for month in range(1, 13)]
    for period, day in book.close.items():
        # the lock date lies in the following month
        assert (day.year, day.month) > (int(period[:4]), int(period[5:])), period
        assert day.weekday() < 5 and day not in HE
    assert set(book.limits) == {"manual", "automatic"}
    assert book.limits["manual"] < book.limits["automatic"]


def test_approval_limit_is_applied(clean_book: Book) -> None:
    """The approval limit is a money amount, the approvals are consistent."""
    book = clean_book
    limit = book.limits["manual"]
    for entry in book.manual():
        total = book.totals.get(entry["doc_no"], 0)
        if total >= limit:
            assert entry["approver_account_id"], entry["doc_no"]


# ---------------------------------------------------------------------------
# near misses of spec section 4
# ---------------------------------------------------------------------------


def _weekend_or_holiday(day: date) -> bool:
    return day.weekday() >= 5 or day in HE


def test_near_miss_automatic_weekend_batches(book: Book) -> None:
    weekend_auto = [
        entry
        for entry in book.entries
        if entry["source"] == "automatic"
        and _weekend_or_holiday(datetime.fromisoformat(entry["entry_ts"]).date())
    ]
    assert len(weekend_auto) > 100


def test_near_miss_bank_working_days(book: Book) -> None:
    for day in (date(2025, 12, 24), date(2025, 12, 31)):
        manual = [
            entry
            for entry in book.manual()
            if datetime.fromisoformat(entry["entry_ts"]).date() == day
        ]
        assert manual, day
        assert not _weekend_or_holiday(day)


def test_near_miss_close_date_itself(book: Book) -> None:
    same_day = [
        entry
        for entry in book.manual()
        if datetime.fromisoformat(entry["entry_ts"]).date()
        == book.close.get(entry["posting_date"][:7])
    ]
    assert len(same_day) >= 3


def test_near_miss_reversal_on_day_11(book: Book) -> None:
    by_original: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in book.entries:
        if entry["reversal_of"]:
            by_original[entry["reversal_of"]].append(entry)
    for originals in by_original.values():
        for reversal in originals:
            if date.fromisoformat(reversal["posting_date"]).day == 11:
                return
    raise AssertionError("no reversal on day 11 of the next period")


def test_near_miss_rent_is_monthly(book: Book) -> None:
    groups: Counter = Counter()
    for line in book.lines:
        if line["account_no"] == "5200" and cents(line["debit"]) == 3_842_000:
            groups[book.header(line["doc_no"])["posting_date"][:7]] += 1
    assert len(groups) >= 13


def test_near_miss_one_cent_below_the_limit(book: Book) -> None:
    limit = book.limits["manual"]
    below = [
        entry
        for entry in book.manual()
        if book.totals.get(entry["doc_no"], 0) == limit - 1 and not entry["approver_account_id"]
    ]
    assert below


def test_near_miss_fifteen_percent_round_amounts(book: Book) -> None:
    groups: dict[str, list[int]] = defaultdict(list)
    for entry in book.manual():
        total = book.totals.get(entry["doc_no"], 0)
        if total >= 100_000:
            groups[book.employee(entry["preparer_account_id"])].append(total)
    shares = [
        sum(1 for total in totals if total % 100_000 == 0) / len(totals)
        for totals in groups.values()
        if len(totals) >= 30
    ]
    assert any(0.13 <= share <= 0.20 for share in shares)
    assert min(shares) < 0.12


def test_near_miss_small_sample_with_high_mad(book: Book) -> None:
    groups: dict[str, list[int]] = defaultdict(list)
    manual_docs = {e["doc_no"] for e in book.manual()}
    for line in book.lines:
        if line["doc_no"] in manual_docs:
            employee = book.employee(book.header(line["doc_no"])["preparer_account_id"])
            groups[employee].append(cents(line["debit"]) or cents(line["credit"]))
    small = [values for values in groups.values() if 400 <= len(values) < 1000]
    assert any(mad_first(values) > 0.015 for values in small)


def test_near_miss_number_range_change(book: Book) -> None:
    assert any(entry["number_range"] == "MJ-2026" for entry in book.entries)
    assert any(entry["number_range"] == "MJ-2025" for entry in book.entries)


# ---------------------------------------------------------------------------
# determinism, module A identity, speed
# ---------------------------------------------------------------------------


def _hash_files(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }


def _without_second_account(directory: Path, name: str) -> bytes:
    """Module C appends one personal AD account (the newest AD id) to three module A files."""
    accounts = read_csv(directory, "accounts.csv")
    extra = max(a["account_id"] for a in accounts if a["system"] == "AD")
    lines = (directory / name).read_bytes().splitlines(keepends=True)
    return b"".join(line for line in lines if extra not in line.decode().rstrip("\n").split(","))


def test_module_a_output_is_byte_identical(tmp_path: Path) -> None:
    # Module C adds exactly one account (+ role assignment + recertification
    # decision); without those rows the files are the ones from before.
    appended = {"accounts.csv", "account_roles.csv", "recert_decisions.csv"}
    for seed, expected in MODULE_A_HASHES.items():
        directory = generate_bank(tmp_path / f"seed{seed}", seed=seed)
        for name in MODULE_A_FILES:
            if name in appended:
                content = _without_second_account(directory, name)
            else:
                content = (directory / name).read_bytes()
            assert hashlib.sha256(content).hexdigest() == expected[name], f"{seed}/{name}"


def test_determinism_is_independent_of_hash_seed(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from audit_workbench.synth import generate_bank\n"
        "generate_bank(sys.argv[1], seed=7)\n"
    )
    hashes = []
    for hash_seed in ("1", "999"):
        target = tmp_path / f"hash{hash_seed}"
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        root = Path(__file__).resolve().parents[2]
        subprocess.run([sys.executable, "-c", script, str(target)], check=True, env=env, cwd=root)
        hashes.append(_hash_files(target))
    assert hashes[0] == hashes[1]


def test_generation_is_fast(tmp_path: Path) -> None:
    start = time.monotonic()
    generate_bank(tmp_path / "fast", seed=13)
    assert time.monotonic() - start < 10.0
