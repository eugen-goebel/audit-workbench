"""Load the journal input files and build the views the rules share.

Rules work on a `JournalData` object instead of raw frames. It parses dates,
timestamps and amounts once, maps GL accounts to employees (a preparer is an
*employee*, not an account) and checks that every entry balances. Each rule
still reports the hashes of the evidence files it used.

Amounts are kept as integer cents and as `Decimal`, never as floats, so
comparisons such as "at or above the limit" or "multiple of 1,000.00" are
exact. Floats appear only inside statistics (Benford, anomaly score).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cached_property
from itertools import product
from pathlib import Path

import pandas as pd

from audit_workbench.access.data import SCHEMA as _ACCESS_SCHEMA
from audit_workbench.core.evidence import Evidence, EvidenceError, load_evidence

AS_OF = date(2026, 2, 28)
"""End of the extract: the last day an entry timestamp can have (spec section 1)."""

SCHEMA: dict[str, list[str]] = {
    "gl_accounts": ["account_no", "name", "account_class", "manual_posting"],
    "journal_entries": [
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
    "journal_lines": ["doc_no", "line_no", "account_no", "debit", "credit"],
    "close_calendar": ["period", "close_date"],
    "approval_limits": ["source", "limit"],
    # module A files: they map GL accounts to employees and give org unit and job function
    "hr_employees": _ACCESS_SCHEMA["hr_employees"],
    "accounts": _ACCESS_SCHEMA["accounts"],
}

_DOC_NO = re.compile(r"^(.+)-(\d{6})$")
_AMOUNT = r"\d{1,15}(\.\d{1,2})?"
_SOURCES = {"manual", "automatic"}
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def load_journal_evidence(directory: Path) -> dict[str, Evidence]:
    """Load every file of the journal schema (and the module A files) from one directory."""
    directory = Path(directory)
    return {
        name: load_evidence(directory / f"{name}.csv", name=name, required_columns=cols)
        for name, cols in SCHEMA.items()
    }


# --- parsing helpers --------------------------------------------------------


def _preview(values: pd.Series, limit: int = 5) -> str:
    shown = [repr(v) for v in list(values.unique())[:limit]]
    return ", ".join(shown) + (", ..." if values.nunique() > limit else "")


def cents(series: pd.Series, *, where: str, allow_empty: bool = False) -> pd.Series:
    """Parse decimal strings like ``1234.50`` to exact integer cents.

    Raises `EvidenceError` for anything that is not a non-negative decimal with
    at most two places. Empty strings count as zero only if `allow_empty`.
    """
    if series.empty:
        return pd.Series([], index=series.index, dtype="int64")
    text = series.astype(str)
    valid = text.str.fullmatch(_AMOUNT) | (text.eq("") if allow_empty else False)
    if not valid.all():
        raise EvidenceError(
            f"{where}: expected a non-negative decimal with up to two places, "
            f"got {_preview(text[~valid])}"
        )
    parts = text.str.partition(".")
    whole = parts[0].where(parts[0] != "", "0").astype("int64")
    frac = parts[2].str.ljust(2, "0").where(parts[2] != "", "00").astype("int64")
    return (whole * 100 + frac).astype("int64")


def _decimals(cents_series: pd.Series) -> pd.Series:
    return pd.Series(
        [Decimal(int(c)).scaleb(-2) for c in cents_series], index=cents_series.index, dtype=object
    )


def _dates(series: pd.Series, *, where: str) -> pd.Series:
    try:
        values = [date.fromisoformat(v) for v in series]
    except ValueError as exc:
        raise EvidenceError(f"{where}: invalid ISO date: {exc}") from exc
    return pd.Series(values, index=series.index, dtype=object)


def _timestamps(series: pd.Series, *, where: str, allow_empty: bool = False) -> pd.Series:
    text = series.astype(str)
    empty = text.eq("")
    if empty.any() and not allow_empty:
        raise EvidenceError(f"{where}: empty timestamp")
    try:
        return pd.to_datetime(text.where(~empty, None), format=_TS_FORMAT)
    except ValueError as exc:
        raise EvidenceError(f"{where}: invalid timestamp: {exc}") from exc


def _object_column(values: list, index: pd.Index) -> pd.Series:
    return pd.Series(values, index=index, dtype=object)


@dataclass(frozen=True)
class JournalData:
    evidence: Mapping[str, Evidence]
    as_of: date = AS_OF

    def __post_init__(self) -> None:
        # Data errors are reported at load time, not by a rule.
        _ = self.entries
        _ = self.lines
        _ = self.gl_accounts
        _ = self.approval_limits_cents
        _ = self.close_dates
        self._check_balance()

    def hashes(self, *names: str) -> list[str]:
        return [self.evidence[n].sha256 for n in names]

    # --- master data ----------------------------------------------------

    @cached_property
    def gl_accounts(self) -> pd.DataFrame:
        df = self.evidence["gl_accounts"].frame.copy()
        flags = df["manual_posting"].str.lower()
        bad = ~flags.isin(["true", "false"])
        if bad.any():
            raise EvidenceError(
                f"gl_accounts.manual_posting: expected true/false, got {_preview(flags[bad])}"
            )
        df["manual_posting"] = flags == "true"
        return df.set_index("account_no", drop=False)

    @cached_property
    def employees(self) -> pd.DataFrame:
        return self.evidence["hr_employees"].frame.set_index("employee_id", drop=False)

    @cached_property
    def account_owner(self) -> dict[str, str]:
        """GL (or any) account id -> employee id, for accounts that belong to a person."""
        df = self.evidence["accounts"].frame
        df = df[df["employee_id"] != ""]
        return dict(zip(df["account_id"], df["employee_id"], strict=True))

    @cached_property
    def close_dates(self) -> dict[str, date]:
        """Period (``2025-03``) -> close date."""
        df = self.evidence["close_calendar"].frame
        parsed = _dates(df["close_date"], where="close_calendar.close_date")
        return dict(zip(df["period"], parsed, strict=True))

    @cached_property
    def approval_limits_cents(self) -> dict[str, int]:
        df = self.evidence["approval_limits"].frame
        values = cents(df["limit"], where="approval_limits.limit")
        return dict(zip(df["source"], (int(v) for v in values), strict=True))

    @property
    def manual_limit_cents(self) -> int:
        """The approval limit for manual entries, in cents."""
        try:
            return self.approval_limits_cents["manual"]
        except KeyError:
            raise EvidenceError("approval_limits: no limit for source 'manual'") from None

    # --- entries ----------------------------------------------------------

    @cached_property
    def entries(self) -> pd.DataFrame:
        """Every header row of the file, duplicates included (B-06 needs them)."""
        df = self.evidence["journal_entries"].frame.copy()

        parts = (
            df["doc_no"].str.extract(_DOC_NO)
            if len(df)
            else pd.DataFrame({0: pd.Series(dtype=str), 1: pd.Series(dtype=str)})
        )
        malformed = parts[0].isna()
        if malformed.any():
            raise EvidenceError(
                f"journal_entries.doc_no: expected <number_range>-<6 digits>, "
                f"got {_preview(df['doc_no'][malformed])}"
            )
        wrong_range = parts[0] != df["number_range"]
        if wrong_range.any():
            raise EvidenceError(
                f"journal_entries.number_range: does not match doc_no for "
                f"{_preview(df['doc_no'][wrong_range])}"
            )
        bad_source = ~df["source"].isin(_SOURCES)
        if bad_source.any():
            raise EvidenceError(
                f"journal_entries.source: unknown {_preview(df['source'][bad_source])}"
            )

        df["counter"] = parts[1].astype("int64")
        df["posting_date"] = _dates(df["posting_date"], where="journal_entries.posting_date")
        df["entry_ts"] = _timestamps(df["entry_ts"], where="journal_entries.entry_ts")
        df["approved_ts"] = _timestamps(
            df["approved_ts"], where="journal_entries.approved_ts", allow_empty=True
        )

        owner = self.account_owner
        df["preparer_employee_id"] = _object_column(
            [owner.get(a) for a in df["preparer_account_id"]], df.index
        )
        df["approver_employee_id"] = _object_column(
            [owner.get(a) for a in df["approver_account_id"]], df.index
        )

        debit_total = self.lines.groupby("doc_no")["debit_cents"].sum()
        df["amount_cents"] = df["doc_no"].map(debit_total).fillna(0).astype("int64")
        df["amount"] = _decimals(df["amount_cents"])
        return df.reset_index(drop=True)

    @cached_property
    def unique_entries(self) -> pd.DataFrame:
        """Entries with one header per doc_no (the first in file order)."""
        return self.entries.drop_duplicates("doc_no", keep="first").reset_index(drop=True)

    @cached_property
    def manual_entries(self) -> pd.DataFrame:
        """The population of most rules: entries keyed by a person."""
        e = self.unique_entries
        return e[e["source"] == "manual"].reset_index(drop=True)

    # --- lines ------------------------------------------------------------

    @cached_property
    def lines(self) -> pd.DataFrame:
        df = self.evidence["journal_lines"].frame.copy()
        df["debit_cents"] = cents(df["debit"], where="journal_lines.debit", allow_empty=True)
        df["credit_cents"] = cents(df["credit"], where="journal_lines.credit", allow_empty=True)
        df["debit"] = _decimals(df["debit_cents"])
        df["credit"] = _decimals(df["credit_cents"])
        # "debit or credit, whichever is not zero"
        df["amount_cents"] = df["debit_cents"] + df["credit_cents"]
        return df

    def _check_balance(self) -> None:
        sums = self.lines.groupby("doc_no")[["debit_cents", "credit_cents"]].sum()
        bad = sums[sums["debit_cents"] != sums["credit_cents"]]
        if not bad.empty:
            shown = ", ".join(sorted(bad.index)[:10])
            more = f" (and {len(bad) - 10} more)" if len(bad) > 10 else ""
            raise EvidenceError(
                f"journal_lines: {len(bad)} entries do not balance (debits != credits): "
                f"{shown}{more}"
            )

    @cached_property
    def amount_lines(self) -> pd.DataFrame:
        """Line amounts with source and preparer, one row per line, for the Benford tests."""
        head = self.unique_entries[["doc_no", "source", "preparer_employee_id"]]
        merged = self.lines.merge(head, on="doc_no", how="inner")
        merged = merged[merged["amount_cents"] > 0]
        return merged[["doc_no", "account_no", "amount_cents", "source", "preparer_employee_id"]]

    @cached_property
    def lines_by_doc(self) -> dict[str, list[tuple[str, int, int]]]:
        """doc_no -> list of ``(account_no, debit_cents, credit_cents)``."""
        out: dict[str, list[tuple[str, int, int]]] = {}
        df = self.lines
        for doc, acc, d, c in zip(
            df["doc_no"], df["account_no"], df["debit_cents"], df["credit_cents"], strict=True
        ):
            out.setdefault(doc, []).append((acc, int(d), int(c)))
        return out

    # --- account pairs (B-09 and the anomaly score) -----------------------

    @cached_property
    def entry_pairs(self) -> dict[str, frozenset[tuple[str, str]]]:
        """doc_no -> every ``(debited account, credited account)`` combination of the entry."""
        out: dict[str, frozenset[tuple[str, str]]] = {}
        for doc in self.unique_entries["doc_no"]:
            lines = self.lines_by_doc.get(doc, [])
            debits = {a for a, d, _ in lines if d > 0}
            credits = {a for a, _, c in lines if c > 0}
            out[doc] = frozenset(product(debits, credits))
        return out

    @cached_property
    def pair_counts(self) -> Counter[tuple[str, str]]:
        """Number of entries (all sources, whole extract) containing each account pair."""
        counts: Counter[tuple[str, str]] = Counter()
        for pairs in self.entry_pairs.values():
            counts.update(pairs)
        return counts


def load_journal_data(directory: Path, as_of: date | None = None) -> JournalData:
    """Load the journal files of one directory; `as_of` defaults to the end of the extract."""
    return JournalData(evidence=load_journal_evidence(directory), as_of=as_of or AS_OF)


__all__ = [
    "AS_OF",
    "SCHEMA",
    "JournalData",
    "cents",
    "load_journal_data",
    "load_journal_evidence",
]
