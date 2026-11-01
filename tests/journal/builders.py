"""A small builder for hand-made journal datasets in tests.

Each rule test describes only the entries it cares about. The builder fills in
defaults, writes the CSV files of the journal schema and loads them, so tests
run against the same loading path as production code.

    ledger = MiniLedger()
    ledger.entry(amount="2500.00", entry_ts="2025-03-08T10:00:00")
    data = ledger.load(tmp_path)
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from audit_workbench.journal.data import SCHEMA, JournalData, load_journal_evidence

AS_OF = date(2026, 2, 28)

DEFAULT_GL_ACCOUNTS = [
    ("1000", "Cash", "asset", "true"),
    ("1200", "Receivables", "asset", "true"),
    ("1600", "Payables", "liability", "true"),
    ("3000", "Equity", "equity", "true"),
    ("4000", "Interest income", "income", "true"),
    ("6000", "Expenses", "expense", "true"),
    ("9999", "Suspense", "suspense", "true"),
]


def _periods() -> list[tuple[str, str]]:
    """Period 2025-01 ... 2026-02, closing on the 10th of the following month."""
    out = []
    for i in range(14):
        year, month = divmod(2025 * 12 + i, 12)
        ny, nm = divmod(year * 12 + month + 1, 12)
        out.append((f"{year}-{month + 1:02d}", f"{ny}-{nm + 1:02d}-10"))
    return out


class MiniLedger:
    def __init__(self, as_of: date = AS_OF) -> None:
        self.as_of = as_of
        self.rows: dict[str, list[dict[str, str]]] = {name: [] for name in SCHEMA}
        self._counters: dict[str, int] = {}
        for no, name, cls, manual in DEFAULT_GL_ACCOUNTS:
            self.gl_account(no, name=name, account_class=cls, manual_posting=manual == "true")
        for period, close in _periods():
            self.close_date(period, close)
        self.limit("manual", "100000.00")
        self.employee("E1")
        self.account("GL-E1", "E1")
        self.account("GL-BATCH")

    # --- master data ----------------------------------------------------

    def gl_account(
        self,
        account_no: str,
        *,
        name: str = "",
        account_class: str = "asset",
        manual_posting: bool = True,
    ) -> MiniLedger:
        self.rows["gl_accounts"].append(
            dict(
                account_no=account_no,
                name=name or f"Account {account_no}",
                account_class=account_class,
                manual_posting="true" if manual_posting else "false",
            )
        )
        return self

    def close_date(self, period: str, close_date: str) -> MiniLedger:
        self.rows["close_calendar"] = [
            r for r in self.rows["close_calendar"] if r["period"] != period
        ]
        self.rows["close_calendar"].append(dict(period=period, close_date=close_date))
        return self

    def limit(self, source: str, limit: str) -> MiniLedger:
        self.rows["approval_limits"] = [
            r for r in self.rows["approval_limits"] if r["source"] != source
        ]
        self.rows["approval_limits"].append(dict(source=source, limit=limit))
        return self

    def employee(
        self,
        employee_id: str,
        *,
        org_unit: str = "Rechnungswesen",
        job_function: str = "Sachbearbeitung",
    ) -> MiniLedger:
        self.rows["hr_employees"].append(
            dict(
                employee_id=employee_id,
                name=f"Person {employee_id}",
                org_unit=org_unit,
                job_function=job_function,
                entry_date="2020-01-01",
                exit_date="",
            )
        )
        return self

    def account(self, account_id: str, employee_id: str = "") -> MiniLedger:
        """A GL user account (or a technical one if `employee_id` is empty)."""
        self.rows["accounts"].append(
            dict(
                account_id=account_id,
                system="GL",
                employee_id=employee_id,
                account_type="personal" if employee_id else "technical",
                interactive="true" if employee_id else "false",
                status="active",
                created_at="2020-01-01T08:00:00",
                last_login="",
            )
        )
        return self

    # --- entries --------------------------------------------------------

    def entry(
        self,
        doc_no: str | None = None,
        *,
        amount: str = "100.00",
        debit: str = "6000",
        credit: str = "1600",
        lines: Sequence[tuple[str, str, str]] | None = None,
        source: str = "manual",
        number_range: str | None = None,
        posting_date: str = "2025-03-10",
        entry_ts: str | None = None,
        preparer: str | None = None,
        approver: str = "",
        approved_ts: str = "",
        description: str = "test",
        reversal_of: str = "",
    ) -> str:
        """Add one header and its lines. Returns the doc_no.

        `lines` is a list of `(account_no, debit, credit)`; without it the entry
        debits `debit` and credits `credit` with `amount`.
        """
        if number_range is None:
            number_range = doc_no.rsplit("-", 1)[0] if doc_no else self._default_range(source)
        if doc_no is None:
            self._counters[number_range] = self._counters.get(number_range, 0) + 1
            doc_no = f"{number_range}-{self._counters[number_range]:06d}"
        if preparer is None:
            preparer = "GL-E1" if source == "manual" else "GL-BATCH"
        self.rows["journal_entries"].append(
            dict(
                doc_no=doc_no,
                number_range=number_range,
                source=source,
                posting_date=posting_date,
                entry_ts=entry_ts if entry_ts is not None else f"{posting_date}T10:00:00",
                preparer_account_id=preparer,
                approver_account_id=approver,
                approved_ts=approved_ts,
                description=description,
                reversal_of=reversal_of,
            )
        )
        if lines is None:
            lines = [(debit, amount, ""), (credit, "", amount)]
        for i, (account_no, d, c) in enumerate(lines, 1):
            self.rows["journal_lines"].append(
                dict(doc_no=doc_no, line_no=str(i), account_no=account_no, debit=d, credit=c)
            )
        return doc_no

    @staticmethod
    def _default_range(source: str) -> str:
        return "MJ-2025" if source == "manual" else "AJ-2025"

    def header(self, doc_no: str, **overrides: str) -> MiniLedger:
        """Append a second header row for an existing doc_no (duplicate)."""
        row = dict(next(r for r in self.rows["journal_entries"] if r["doc_no"] == doc_no))
        row.update(overrides)
        self.rows["journal_entries"].append(row)
        return self

    # --- output ---------------------------------------------------------

    def write(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, columns in SCHEMA.items():
            with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                writer.writerows(self.rows[name])
        return directory

    def load(self, directory: Path) -> JournalData:
        return JournalData(evidence=load_journal_evidence(self.write(directory)), as_of=self.as_of)
