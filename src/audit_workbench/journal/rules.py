"""Journal-entry audit rules B-01 to B-10.

Ten selection criteria for journal entry testing under ISA 240 (Revised)
para. 48-49. They select entries and groups worth a closer look; they do not
replace inquiry or vouching to supporting documents.

* B-01  A preparer's amounts do not follow Benford's first-digit pattern.
* B-02  A GL account's amounts do not follow Benford's first-two-digits pattern.
* B-03  A preparer posts an unusually high share of round amounts.
* B-04  A manual entry was keyed on a weekend or a public holiday.
* B-05  Post-closing entries and early reversals around the period end.
* B-06  Gaps and duplicates in document number ranges (completeness).
* B-07  The same manual entry was posted twice.
* B-08  A large manual entry was not approved by a second person.
* B-09  A manual entry uses a rare debit/credit combination.
* B-10  A manual entry was keyed by someone who does not usually post.

Unless stated otherwise the population is manual entries: automatic entries
are covered by IT general controls and interface controls.

Each rule exposes ``run(data: JournalData) -> list[Finding]``. The module also
exports ``JOURNAL_RULES`` (a ``RuleRegistry``) and ``run_journal_rules``.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import holidays
import numpy as np
import pandas as pd

from audit_workbench.core.findings import Finding, Severity
from audit_workbench.core.rules import Rule, RuleRegistry
from audit_workbench.journal.benford import THRESHOLDS, benford_groups
from audit_workbench.journal.data import JournalData

ISA_SELECTION = "ISA 240 (Revised) para. 49(d): entries throughout the period"
ISA_PERIOD_END = "ISA 240 (Revised) para. 49(c): entries at the end of a reporting period"
ISA_COMPLETENESS = "ISA 240 (Revised) para. 49(b): completeness of the population"
GOBD_COMPLETENESS = "GoBD (BMF 28.11.2019) Rz. 40: complete recording"

_ROUND_BASE_CENTS = 100_000  # 1000.00
_RISKY_CLASSES = frozenset({"income", "equity", "suspense"})


def _ctx(data: JournalData) -> dict[str, str]:
    return {"as_of": data.as_of.isoformat()}


def _money(cents: int) -> str:
    return str(Decimal(int(cents)).scaleb(-2))


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: f.entity)


# ---------------------------------------------------------------------------
# B-01  benford_first_digit
# ---------------------------------------------------------------------------


class BenfordFirstDigit(Rule):
    """B-01: First-digit distribution of a preparer's amounts departs from Benford.

    An auditor uses it to find preparers whose manual amounts look invented:
    people choosing numbers produce a flatter first-digit pattern than amounts
    that arise from business transactions. Statistic: MAD (Nigrini 2012) with
    the nonconformity limit 0.015. Small groups are not judged, because the MAD
    of even a perfectly conforming sample grows as the sample shrinks.
    """

    rule_id: str = "B-01"
    version: str = "1.0.0"
    title: str = "Benford first-digit test per preparer"

    def __init__(self, *, min_n: int = 1000, **kwargs: Any) -> None:
        super().__init__(min_n=min_n, **kwargs)
        self.min_n = min_n

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        limit = THRESHOLDS[1][2]
        findings = [
            self.finding(
                severity=Severity.MEDIUM,
                entity=employee_id,
                description=(
                    f"First digits of {result.n} manual line amounts of employee {employee_id} "
                    f"deviate from Benford's law (MAD {result.mad:.4f} > {limit})."
                ),
                evidence=data.hashes("journal_entries", "journal_lines", "accounts"),
                details=result.details(),
                regulation_refs=[ISA_SELECTION],
                context=_ctx(data),
            )
            for employee_id, result in benford_groups(data, "employee", digits=1).items()
            if result.n >= self.min_n and result.mad > limit
        ]
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-02  benford_first_two
# ---------------------------------------------------------------------------


class BenfordFirstTwo(Rule):
    """B-02: First-two-digits distribution of a GL account's amounts departs from Benford.

    An auditor uses it to find accounts fed with invented or manipulated
    amounts, for example fee income that is posted in made-up round figures.
    Statistic: MAD with the nonconformity limit 0.0022. The test needs many
    amounts (90 digit classes), hence the high default for `min_n`.
    """

    rule_id: str = "B-02"
    version: str = "1.0.0"
    title: str = "Benford first-two-digits test per GL account"

    def __init__(self, *, min_n: int = 5000, **kwargs: Any) -> None:
        super().__init__(min_n=min_n, **kwargs)
        self.min_n = min_n

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        limit = THRESHOLDS[2][2]
        findings = [
            self.finding(
                severity=Severity.MEDIUM,
                entity=account_no,
                description=(
                    f"First two digits of {result.n} line amounts on account {account_no} "
                    f"deviate from Benford's law (MAD {result.mad:.5f} > {limit})."
                ),
                evidence=data.hashes("journal_entries", "journal_lines"),
                details=result.details(),
                regulation_refs=[ISA_SELECTION],
                context=_ctx(data),
            )
            for account_no, result in benford_groups(data, "account", digits=2).items()
            if result.n >= self.min_n and result.mad > limit
        ]
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-03  round_amounts
# ---------------------------------------------------------------------------


class RoundAmounts(Rule):
    """B-03: A preparer posts an unusually high share of round amounts.

    An auditor uses it to find estimates and plug figures: amounts that are
    exact multiples of 1,000.00 are rare in transaction-driven postings and
    common in manual adjustments. Only entries of at least 1,000.00 count, so
    small round amounts do not dilute the share.
    """

    rule_id: str = "B-03"
    version: str = "1.0.0"
    title: str = "High share of round amounts per preparer"

    def __init__(self, *, min_entries: int = 30, round_share: float = 0.25, **kwargs: Any) -> None:
        super().__init__(min_entries=min_entries, round_share=round_share, **kwargs)
        self.min_entries = min_entries
        self.round_share = round_share

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        m = data.manual_entries
        m = m[m["preparer_employee_id"].notna() & (m["amount_cents"] >= _ROUND_BASE_CENTS)]
        m = m.assign(is_round=(m["amount_cents"] % _ROUND_BASE_CENTS) == 0)
        threshold = Decimal(str(self.round_share))  # exact: 0.15 is 15 %, not 0.1499...

        findings: list[Finding] = []
        for employee_id, group in m.groupby("preparer_employee_id", sort=True):
            entries = len(group)
            rounds = int(group["is_round"].sum())
            if entries < self.min_entries or rounds <= threshold * entries:
                continue
            findings.append(
                self.finding(
                    severity=Severity.LOW,
                    entity=str(employee_id),
                    description=(
                        f"{rounds} of {entries} manual entries of at least 1000.00 by employee "
                        f"{employee_id} are multiples of 1000.00 ({rounds / entries:.0%})."
                    ),
                    evidence=data.hashes("journal_entries", "journal_lines", "accounts"),
                    details={
                        "entries": entries,
                        "round_entries": rounds,
                        "share": rounds / entries,
                    },
                    regulation_refs=[ISA_SELECTION],
                    context=_ctx(data),
                )
            )
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-04  off_hours
# ---------------------------------------------------------------------------


class OffHours(Rule):
    """B-04: Manual entry keyed on a weekend or a public holiday in Hessen.

    An auditor uses it to select entries made outside normal working time,
    when supervision is weakest. The entry timestamp counts, not the posting
    date. 24 and 31 December are working days for a bank. Automatic batches
    run every night and are not tested.
    """

    rule_id: str = "B-04"
    version: str = "1.0.0"
    title: str = "Manual entry on weekend or public holiday"

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        m = data.manual_entries
        if m.empty:
            return []
        years = sorted(m["entry_ts"].dt.year.unique())
        calendar_he = holidays.Germany(subdiv="HE", years=[int(y) for y in years], language="de")

        findings = []
        for doc_no, ts in zip(m["doc_no"], m["entry_ts"], strict=True):
            holiday = calendar_he.get(ts.date())
            if ts.weekday() < 5 and holiday is None:
                continue
            findings.append(
                self.finding(
                    severity=Severity.MEDIUM,
                    entity=doc_no,
                    description=(
                        f"Entry {doc_no} was keyed on {ts.day_name()} {ts.date()}"
                        + (f" ({holiday})." if holiday else ".")
                    ),
                    evidence=data.hashes("journal_entries"),
                    details={
                        "entry_ts": ts.isoformat(),
                        "weekday": ts.day_name(),
                        "holiday": holiday,
                    },
                    regulation_refs=[ISA_PERIOD_END],
                    context=_ctx(data),
                )
            )
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-05  period_end
# ---------------------------------------------------------------------------


class PeriodEnd(Rule):
    """B-05: Post-closing entries and early reversals of period-end entries.

    An auditor uses it for entries made at the end of a reporting period
    (ISA 240 para. 49(c)). A post-closing entry was keyed after the period was
    locked. An early reversal is an entry in the last days of a period that is
    reversed in the first days of the next one, a pattern for pulling revenue
    forward into the period.
    """

    rule_id: str = "B-05"
    version: str = "1.0.0"
    title: str = "Post-closing entries and early reversals"

    def __init__(self, *, end_days: int = 5, reversal_days: int = 10, **kwargs: Any) -> None:
        super().__init__(end_days=end_days, reversal_days=reversal_days, **kwargs)
        self.end_days = end_days
        self.reversal_days = reversal_days

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        reversals: dict[str, list[tuple[date, str]]] = defaultdict(list)
        everything = data.unique_entries
        rev = everything[everything["reversal_of"] != ""]
        for original, posted, doc_no in zip(
            rev["reversal_of"], rev["posting_date"], rev["doc_no"], strict=True
        ):
            reversals[original].append((posted, doc_no))

        findings = []
        for doc_no, posted, ts in zip(
            data.manual_entries["doc_no"],
            data.manual_entries["posting_date"],
            data.manual_entries["entry_ts"],
            strict=True,
        ):
            triggers: list[str] = []
            close = data.close_dates.get(f"{posted.year}-{posted.month:02d}")
            days_after_close = None
            if close is not None and ts.date() > close:
                triggers.append("post_closing")
                days_after_close = (ts.date() - close).days

            reversed_by = self._early_reversal(posted, sorted(reversals.get(doc_no, [])))
            if reversed_by is not None:
                triggers.append("early_reversal")
            if not triggers:
                continue

            post_closing = "post_closing" in triggers
            findings.append(
                self.finding(
                    severity=Severity.HIGH if post_closing else Severity.MEDIUM,
                    entity=doc_no,
                    description=(
                        f"Entry {doc_no} (posting date {posted}): "
                        + " and ".join(t.replace("_", " ") for t in triggers)
                        + "."
                    ),
                    evidence=data.hashes("journal_entries", "close_calendar"),
                    details={
                        "triggers": triggers,
                        "close_date": close.isoformat() if close else None,
                        "days_after_close": days_after_close,
                        "reversed_by": reversed_by,
                    },
                    regulation_refs=[ISA_PERIOD_END],
                    context=_ctx(data),
                )
            )
        return _sorted(findings)

    def _early_reversal(self, posted: date, reversals: list[tuple[date, str]]) -> str | None:
        """doc_no of the first reversal in the first days of the next period, if the entry
        was posted in the last days of its period."""
        last_day = calendar.monthrange(posted.year, posted.month)[1]
        if posted.day <= last_day - self.end_days:
            return None
        next_year, next_month = (
            (posted.year + 1, 1) if posted.month == 12 else (posted.year, posted.month + 1)
        )
        for reversal_date, reversal_doc in reversals:
            if (reversal_date.year, reversal_date.month) == (
                next_year,
                next_month,
            ) and reversal_date.day <= self.reversal_days:
                return reversal_doc
        return None


# ---------------------------------------------------------------------------
# B-06  doc_number_completeness
# ---------------------------------------------------------------------------


class DocNumberCompleteness(Rule):
    """B-06: Gaps and duplicates in document number ranges.

    An auditor uses it to obtain evidence about the completeness of the
    population of journal entries (ISA 240 para. 49(b)): a missing number may
    be a deleted entry, a duplicate number may hide a second, different entry.
    All entries, manual and automatic, are checked per number range, so the
    change from ``MJ-2025`` to ``MJ-2026`` is not a gap.
    """

    rule_id: str = "B-06"
    version: str = "1.0.0"
    title: str = "Gaps and duplicates in document numbers"

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        findings: list[Finding] = []
        refs = [ISA_COMPLETENESS, GOBD_COMPLETENESS]
        for number_range, group in data.entries.groupby("number_range", sort=True):
            counters = np.sort(group["counter"].unique())
            jumps = np.nonzero(np.diff(counters) > 1)[0]
            for i in jumps:
                first, last = int(counters[i]) + 1, int(counters[i + 1]) - 1
                findings.append(
                    self.finding(
                        severity=Severity.HIGH,
                        entity=f"{number_range}:gap:{first:06d}-{last:06d}",
                        description=(
                            f"Document numbers {number_range}-{first:06d} to "
                            f"{number_range}-{last:06d} are missing ({last - first + 1})."
                        ),
                        evidence=data.hashes("journal_entries"),
                        details={
                            "kind": "gap",
                            "number_range": number_range,
                            "first_missing": f"{number_range}-{first:06d}",
                            "last_missing": f"{number_range}-{last:06d}",
                            "missing_count": last - first + 1,
                        },
                        regulation_refs=refs,
                        context=_ctx(data),
                    )
                )
            occurrences = group["doc_no"].value_counts()
            for doc_no, count in occurrences[occurrences > 1].items():
                findings.append(
                    self.finding(
                        severity=Severity.HIGH,
                        entity=f"{number_range}:dup:{doc_no}",
                        description=f"Document number {doc_no} appears in {count} entry headers.",
                        evidence=data.hashes("journal_entries"),
                        details={
                            "kind": "dup",
                            "number_range": number_range,
                            "doc_no": doc_no,
                            "occurrences": int(count),
                        },
                        regulation_refs=refs,
                        context=_ctx(data),
                    )
                )
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-07  duplicate_entries
# ---------------------------------------------------------------------------


class DuplicateEntries(Rule):
    """B-07: The same manual entry was posted twice.

    An auditor uses it to find double postings, which overstate expense or
    income and sometimes cover a diverted payment. Two entries are duplicates
    if they have the same amount and the same lines and their posting dates are
    close together. A recurring entry such as monthly rent is outside the
    window, and a reversal is not a duplicate of the entry it reverses.
    """

    rule_id: str = "B-07"
    version: str = "1.0.0"
    title: str = "Same entry posted twice"

    def __init__(self, *, window_days: int = 7, **kwargs: Any) -> None:
        super().__init__(window_days=window_days, **kwargs)
        self.window_days = window_days

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        m = data.manual_entries
        groups: dict[tuple, list[tuple]] = defaultdict(list)
        for doc_no, amount, posted, ts, reversal_of in zip(
            m["doc_no"],
            m["amount_cents"],
            m["posting_date"],
            m["entry_ts"],
            m["reversal_of"],
            strict=True,
        ):
            lines = data.lines_by_doc.get(doc_no)
            if not lines:
                continue
            signature = (int(amount), tuple(sorted(lines)))
            groups[signature].append((ts, doc_no, posted, reversal_of))

        findings = []
        for signature, members in groups.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda x: (x[0], x[1]))
            for j in range(1, len(members)):
                _, later_doc, later_date, later_rev = members[j]
                partners = [
                    (doc, abs((later_date - posted).days))
                    for _, doc, posted, rev in members[:j]
                    if abs((later_date - posted).days) <= self.window_days
                    and rev != later_doc
                    and later_rev != doc
                ]
                if not partners:
                    continue
                findings.append(
                    self.finding(
                        severity=Severity.MEDIUM,
                        entity=later_doc,
                        description=(
                            f"Entry {later_doc} repeats {partners[0][0]} "
                            f"(amount {_money(signature[0])}, same lines, "
                            f"{partners[0][1]} days apart)."
                        ),
                        evidence=data.hashes("journal_entries", "journal_lines"),
                        details={
                            "duplicate_of": [doc for doc, _ in partners],
                            "days_apart": partners[0][1],
                            "amount": _money(signature[0]),
                        },
                        regulation_refs=[ISA_SELECTION],
                        context=_ctx(data),
                    )
                )
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-08  approval_missing
# ---------------------------------------------------------------------------


class ApprovalMissing(Rule):
    """B-08: Large manual entry without a second pair of eyes.

    An auditor uses it to test the four-eyes principle for manual entries at
    or above the approval limit: no approver, or an approver who is the same
    *employee* as the preparer. A second GL account of the same person does not
    make a second pair of eyes (the cross-account view from module A).
    """

    rule_id: str = "B-08"
    version: str = "1.0.0"
    title: str = "Manual entry above the limit not approved by a second person"

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        limit = data.manual_limit_cents
        m = data.manual_entries
        m = m[m["amount_cents"] >= limit]

        findings = []
        for row in m.itertuples():
            if row.approver_account_id == "":
                trigger = "missing"
            elif (
                row.preparer_employee_id is not None
                and row.preparer_employee_id == row.approver_employee_id
            ):
                trigger = "self_approval"
            else:
                continue
            what = (
                "has no approver"
                if trigger == "missing"
                else f"was approved by the same employee ({row.approver_employee_id})"
            )
            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=row.doc_no,
                    description=(
                        f"Entry {row.doc_no} of {_money(row.amount_cents)} (limit "
                        f"{_money(limit)}) {what}."
                    ),
                    evidence=data.hashes(
                        "journal_entries", "journal_lines", "accounts", "approval_limits"
                    ),
                    details={
                        "trigger": trigger,
                        "preparer_employee_id": row.preparer_employee_id,
                        "approver_employee_id": row.approver_employee_id,
                        "amount": _money(row.amount_cents),
                        "limit": _money(limit),
                    },
                    regulation_refs=[ISA_SELECTION],
                    context=_ctx(data),
                )
            )
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-09  unusual_account_pair
# ---------------------------------------------------------------------------


class UnusualAccountPair(Rule):
    """B-09: Manual entry with a rare debit and credit combination.

    An auditor uses it to find postings that business processes do not
    normally produce, for example debit receivables / credit interest income
    keyed by hand. The frequency of each account pair is counted over all
    entries of the extract; only rare pairs that involve an income, equity or
    suspense account are reported.
    """

    rule_id: str = "B-09"
    version: str = "1.0.0"
    title: str = "Rare debit/credit account combination"

    def __init__(self, *, max_count: int = 3, **kwargs: Any) -> None:
        super().__init__(max_count=max_count, **kwargs)
        self.max_count = max_count

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        classes = data.gl_accounts["account_class"]
        counts = data.pair_counts

        findings = []
        for doc_no in data.manual_entries["doc_no"]:
            rare = sorted(
                (debit, credit, counts[(debit, credit)])
                for debit, credit in data.entry_pairs[doc_no]
                if counts[(debit, credit)] < self.max_count
                and (classes.get(debit) in _RISKY_CLASSES or classes.get(credit) in _RISKY_CLASSES)
            )
            if not rare:
                continue
            findings.append(
                self.finding(
                    severity=Severity.MEDIUM,
                    entity=doc_no,
                    description=(
                        f"Entry {doc_no} debits {rare[0][0]} against {rare[0][1]}, a combination "
                        f"found in only {rare[0][2]} entries of the extract."
                    ),
                    evidence=data.hashes("journal_entries", "journal_lines", "gl_accounts"),
                    details={
                        "pairs": [
                            {"debit_account": d, "credit_account": c, "entries": n}
                            for d, c, n in rare
                        ]
                    },
                    regulation_refs=[ISA_SELECTION],
                    context=_ctx(data),
                )
            )
        return _sorted(findings)


# ---------------------------------------------------------------------------
# B-10  unexpected_preparer
# ---------------------------------------------------------------------------


class UnexpectedPreparer(Rule):
    """B-10: Manual entry by someone who does not usually post.

    An auditor uses it to find entries keyed by employees outside the
    accounting department, or by system administrators who also hold GL
    access. The preparer is the employee behind the GL account, taken from the
    HR master data. Accounts without an HR record cannot be judged here
    (module A reports them as orphan accounts).
    """

    rule_id: str = "B-10"
    version: str = "1.0.0"
    title: str = "Manual entry by an unexpected preparer"

    def __init__(self, *, posting_units: list[str] | None = None, **kwargs: Any) -> None:
        units = list(posting_units) if posting_units is not None else ["Rechnungswesen"]
        super().__init__(posting_units=units, **kwargs)
        self.posting_units = units

    def run(self, data: JournalData) -> list[Finding]:  # type: ignore[override]
        hr = data.employees
        findings = []
        for doc_no, employee_id in zip(
            data.manual_entries["doc_no"],
            data.manual_entries["preparer_employee_id"],
            strict=True,
        ):
            if employee_id is None or employee_id not in hr.index:
                continue
            org_unit = hr.at[employee_id, "org_unit"]
            job_function = hr.at[employee_id, "job_function"]
            triggers = []
            if org_unit not in self.posting_units:
                triggers.append("org_unit")
            if job_function == "Administration":
                triggers.append("job_function")
            if not triggers:
                continue
            findings.append(
                self.finding(
                    severity=Severity.HIGH,
                    entity=doc_no,
                    description=(
                        f"Entry {doc_no} was keyed by employee {employee_id} "
                        f"({org_unit}, {job_function})."
                    ),
                    evidence=data.hashes("journal_entries", "accounts", "hr_employees"),
                    details={
                        "triggers": triggers,
                        "preparer_employee_id": employee_id,
                        "org_unit": org_unit,
                        "job_function": job_function,
                    },
                    regulation_refs=[ISA_SELECTION],
                    context=_ctx(data),
                )
            )
        return _sorted(findings)


# ---------------------------------------------------------------------------
# Registry and convenience function
# ---------------------------------------------------------------------------

JOURNAL_RULES = RuleRegistry()
for _cls in [
    BenfordFirstDigit,
    BenfordFirstTwo,
    RoundAmounts,
    OffHours,
    PeriodEnd,
    DocNumberCompleteness,
    DuplicateEntries,
    ApprovalMissing,
    UnusualAccountPair,
    UnexpectedPreparer,
]:
    JOURNAL_RULES.register(_cls)


def run_journal_rules(
    data: JournalData,
    params: dict[str, dict[str, Any]] | None = None,
    **overrides: dict[str, Any],
) -> list[Finding]:
    """Run all ten journal rules and return the findings sorted by rule and entity.

    Parameters per rule come as a mapping ``{"B-03": {"min_entries": 10}}`` or as
    keyword arguments (``run_journal_rules(data, **{"B-03": {"min_entries": 10}})``).
    Unknown rule ids raise `KeyError`.
    """
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (params or {}).items()}
    for key, value in overrides.items():
        merged.setdefault(key.replace("_", "-").upper(), {}).update(value)
    unknown = sorted(set(merged) - set(JOURNAL_RULES.ids()))
    if unknown:
        raise KeyError(f"unknown journal rule id(s): {', '.join(unknown)}")
    findings = JOURNAL_RULES.run_all(data, merged)
    return sorted(findings, key=lambda f: (f.rule_id, f.entity))


_ = pd  # pandas types appear in the annotations of the data layer
