# Module B: Journal entry testing

Design spec for phase 2. Rules, data model and the synthetic ledger are built
against this document. If code and spec disagree, the spec is fixed first.

Status: approved 2026-11-01.

## 0. Why these tests

ISA 240 (Revised), effective for periods beginning on or after 15 December
2026, requires the auditor to test the appropriateness of journal entries and
other adjustments (para. 48). In doing so the auditor shall obtain evidence
about the **completeness of the population** of journal entries (para. 49(b)),
select entries **made at the end of a reporting period** (49(c)), and determine
the need to test entries made **throughout the period** (49(d)).

This module covers the data-driven part: completeness of the population
(B-06), end-of-period and post-closing entries (B-05), and selection by risk
characteristics across the full year (all other rules). It does not replace
inquiry or vouching to supporting documents.

Completeness checks on document numbers (gap and duplicate analysis) are also
named by the GoBD (BMF letter of 28 November 2019 as amended,
Rz. 40) as an example of a control that ensures complete recording.

## 1. Fiscal year and dates

- Fiscal year = calendar year **2025**. Period = calendar month (`2025-01` ... `2025-12`).
- Each period has a close date in `close_calendar.csv`. After the close date the period is locked; an entry with a posting date in that period and an entry timestamp after the close date is a **post-closing entry**.
- The extract contains all entries with an entry timestamp up to **2026-02-28**, so January and February 2026 postings are included. This is where post-closing entries and early reversals show up.
- `as_of` for module B is the end of the extract, `2026-02-28`.
- The ledger belongs to the same synthetic bank as module A. Every human GL user is a `GL-` account in `accounts.csv` and therefore maps to an employee. A preparer is identified as an **employee**, not as an account, exactly as in module A.

## 2. Input files

Same conventions as module A: CSV, UTF-8, read as text, ISO dates and
timestamps without timezone, amounts as decimal strings with two places
(`1234.50`), never floats in the files.

| Evidence name | File | Columns |
|---|---|---|
| `gl_accounts` | `gl_accounts.csv` | `account_no, name, account_class, manual_posting` |
| `journal_entries` | `journal_entries.csv` | `doc_no, number_range, source, posting_date, entry_ts, preparer_account_id, approver_account_id, approved_ts, description, reversal_of` |
| `journal_lines` | `journal_lines.csv` | `doc_no, line_no, account_no, debit, credit` |
| `close_calendar` | `close_calendar.csv` | `period, close_date` |
| `approval_limits` | `approval_limits.csv` | `source, limit` |

Value sets:

- `account_class`: `asset`, `liability`, `equity`, `income`, `expense`, `suspense`. The chart of accounts follows a simplified version of the balance sheet and income statement layouts of the RechKredV, about 120 accounts.
- `manual_posting`: `true` / `false`. Some accounts are fed by subledgers only.
- `source`: `manual` (keyed by a person) or `automatic` (interface or batch from a subledger, preparer is a technical account).
- `number_range`: one range per source and year, for example `MJ-2025` and `AJ-2025`. `doc_no` is `<number_range>-<6-digit counter>`.
- `approver_account_id`, `approved_ts`: empty if the entry was not approved.
- `reversal_of`: `doc_no` of the reversed entry, or empty.
- An entry balances: sum of debits = sum of credits. Unbalanced entries are a data error and reported by the loader, not by a rule.

The **amount** of an entry is its total debit.

## 3. Rules

Severity values come from `core.findings.Severity`. The ground-truth key is
`(rule_id, entity)`. Unless stated otherwise the population is **manual
entries**. Automatic entries are system-generated and covered by IT general
controls and interface controls, not by these selection criteria.

### B-01 `benford_first_digit`: first-digit test per preparer

- **Population:** line amounts (debit or credit, whichever is not zero) of manual entries, grouped by preparer employee. Amounts below `10.00` are excluded, so every amount has at least two significant digits before the decimal point.
- **Statistic:** MAD between observed and Benford first-digit proportions (Nigrini 2012), plus chi-square and the per-digit z-statistic with continuity correction in the details.
- **Exception:** the group has at least `min_n` amounts and MAD > `0.015` (Nigrini's nonconformity threshold for the first digit).
- **Params:** `min_n` (default 1000).
- **Entity:** `employee_id`. **Severity:** MEDIUM.
- **Why `min_n` = 1000:** Nigrini's MAD thresholds do not depend on the sample size, but the MAD of a perfectly conforming sample does. In a simulation with 400 perfectly Benford-distributed samples per size, 36 % of samples of 300 amounts and 8 % of samples of 500 amounts exceeded 0.015. At 1000 amounts none did (99th percentile 0.0125). The simulation is part of the tests.

### B-02 `benford_first_two`: first-two-digits test per GL account

- **Population:** as B-01, grouped by GL account (all sources).
- **Exception:** at least `min_n` amounts and MAD > `0.0022` (Nigrini's first-two-digits nonconformity threshold).
- **Params:** `min_n` (default 5000).
- **Entity:** `account_no`. **Severity:** MEDIUM.
- **Why `min_n` = 5000:** in the same simulation, 91 % of conforming samples of 1000 amounts exceeded 0.0022; at 2000 amounts 0.3 %; at 5000 none (99th percentile 0.0014).

### B-03 `round_amounts`: share of round amounts per preparer

- **Exception:** a preparer has at least `min_entries` manual entries with an amount of at least `1000.00`, and more than `round_share` of those amounts are multiples of `1000.00`.
- **Params:** `min_entries` (default 30), `round_share` (default 0.25).
- **Entity:** `employee_id`. **Severity:** LOW.

### B-04 `off_hours`: manual entry on a weekend or public holiday

- **Exception:** the date of `entry_ts` is a Saturday, Sunday or public holiday in Hessen (package `holidays`, subdivision `HE`).
- **Entity:** `doc_no`. **Severity:** MEDIUM.
- 24 and 31 December are working days for a bank and are not flagged.

### B-05 `period_end`: post-closing entries and early reversals

One finding per entry, if at least one trigger applies:

- **Post-closing:** `entry_ts` is after the close date of the period of `posting_date`.
- **Early reversal:** `posting_date` is in the last `end_days` calendar days of a period and the entry is reversed by an entry with a `posting_date` within the first `reversal_days` days of the next period.

- **Params:** `end_days` (default 5), `reversal_days` (default 10).
- **Entity:** `doc_no` of the original entry. **Severity:** HIGH for post-closing, else MEDIUM.
- **Details:** `triggers`, `close_date`, `days_after_close`, `reversed_by`.

### B-06 `doc_number_completeness`: gaps and duplicates in number ranges

- **Population:** all entries, manual and automatic, per `number_range`.
- **Exception, gap:** a counter between the lowest and the highest counter of the range is missing. Consecutive missing counters form one gap.
- **Exception, duplicate:** the same `doc_no` appears in more than one entry header.
- **Entity:** `<number_range>:gap:<first missing>-<last missing>` (zero-padded as in `doc_no`) or `<number_range>:dup:<doc_no>`. **Severity:** HIGH.

### B-07 `duplicate_entries`: same entry posted twice

- **Exception:** two manual entries, neither reversing the other, with the same amount, the same set of `(account_no, debit, credit)` lines, and posting dates no more than `window_days` apart.
- **Params:** `window_days` (default 7).
- **Entity:** `doc_no` of the later entry (by `entry_ts`, then `doc_no`). **Severity:** MEDIUM.

### B-08 `approval_missing`: four-eyes principle not applied

- **Population:** manual entries with an amount at or above the limit for `manual` in `approval_limits.csv`.
- **Exception:** no approver, or the approver account belongs to the **same employee** as the preparer account. This is the cross-account view from module A: a second account does not make a second pair of eyes.
- **Entity:** `doc_no`. **Severity:** HIGH.
- **Details:** `trigger` (`missing` or `self_approval`), `preparer_employee_id`, `approver_employee_id`.

### B-09 `unusual_account_pair`: rare debit and credit combination

- **Population:** all entries define the frequency base; manual entries are tested.
- **Pair:** for an entry, every combination of a debited account and a credited account.
- **Exception:** a manual entry contains a pair that occurs in fewer than `max_count` entries of the whole year, and the pair involves an `income`, `equity` or `suspense` account.
- **Params:** `max_count` (default 3).
- **Entity:** `doc_no`. **Severity:** MEDIUM.

### B-10 `unexpected_preparer`: manual entry by someone who does not usually post

- **Exception:** the preparer employee's `org_unit` in HR is not in `posting_units`, or the employee's `job_function` is `Administration` (system administrators who also have GL access).
- **Params:** `posting_units` (default `["Rechnungswesen"]`).
- **Entity:** `doc_no`. **Severity:** HIGH.

### Anomaly score (not a rule)

An Isolation Forest (scikit-learn, fixed `random_state`) scores manual entries
on amount, hour, weekday, days to period end, account pair frequency and
preparer volume. The score is a **ranking aid** for sample selection, shown in
the report and in `awb run --rank`. It does not produce findings, is not part
of the ground-truth check, and the README says so. Reason: a score without a
threshold that an auditor can explain is not an audit test.

## 4. Synthetic ledger and ground truth

`generate_bank(...)` additionally writes the files from section 2. The issues
are appended to the same `ground_truth.json`.

Requirements, as in module A:

1. Same seed, same bytes, independent of `PYTHONHASHSEED`.
2. Without injection, every B rule returns zero findings.
3. With injection, the run returns exactly the ground truth.
4. Clean amounts are drawn log-uniform over whole decades (10 to 10,000,000), which is exactly Benford-distributed. Manual entries contain a realistic share of round amounts (about 5 %).
5. Volume: about 60,000 entries and 180,000 lines over the year, about 20 % manual. Automatic batches run every night including weekends.
6. About 20 accounting clerks post most manual entries, so each of them has well over 1000 amounts and B-01 can judge them. Occasional preparers (team leads, analysts) stay below `min_n` and are listed by `awb benford` as too small to judge.

Near misses that must not be flagged:

- automatic batch postings on weekends (B-04 covers manual only)
- a manual entry on 31 December and one on 24 December (bank working days)
- a manual entry entered on the close date itself, not after it (B-05)
- a reversal on day 11 of the next period (B-05)
- a monthly rent entry with the same amount every month (B-07 window)
- a manual entry one cent below the approval limit without an approver (B-08)
- a preparer with 15 % round amounts (B-03)
- a preparer with 600 amounts whose MAD is high only because the sample is small (B-01 `min_n`)
- the number range change from `MJ-2025` to `MJ-2026` (B-06 is per range)

Injected issue counts (default seed):

| Rule | Count | Notes |
|---|---|---|
| B-01 | 1 | one preparer whose manual amounts are invented (uniform first digits) |
| B-02 | 1 | one fee income account fed with invented amounts |
| B-03 | 2 | |
| B-04 | 6 | 2 on public holidays (one of them Whit Monday) |
| B-05 | 8 | 5 post-closing, 3 early reversals of period-end revenue |
| B-06 | 4 | 3 gaps (one of them in the automatic range), 1 duplicate |
| B-07 | 5 | |
| B-08 | 6 | 3 missing, 3 self-approval through a second GL account of the same employee |
| B-09 | 5 | for example debit receivables, credit interest income, manual |
| B-10 | 4 | 2 by an IT administrator |

Total 42. Some entries carry two characteristics on purpose (for example post-closing and self-approved), because real manipulations combine them. Each rule still has to report its own entity.

## 5. CLI

- `awb run` runs modules A and B. `--module A|B` restricts the run.
- `awb check-ground-truth` reports A and B in one table, grouped by module.
- `awb explain` shows for a B finding the entry with all lines, the preparer and approver employees, and for B-01/B-02 the digit table with expected and observed proportions.
- `awb benford DATA_DIR --by employee|account` prints the digit tables for all groups, including the ones below `min_n`, marked as "too small to judge".
