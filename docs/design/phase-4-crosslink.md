# Phase 4: Cross-module findings, sampling, regulation mapping

Design spec for phase 4. Code, generator changes and tests are built against
this document. If code and spec disagree, the spec is fixed first.

Status: approved 2026-11-01.

## 0. Why

Modules A, B and C each test one data set. A finding in module A is a *risk*:
someone could do something they should not. Phase 4 asks whether the risk was
*used*: did the employee who holds two conflicting functions actually post and
approve the same entry? Did someone act in a system after leaving the bank?
Did an account post entries it had no right to post? Audit teams do these
joins by hand, with spreadsheets. Here they are rules with ground truth.

Two smaller parts complete the audit workflow:

- **Sampling** for the manual control tests that rules cannot do (is the test
  evidence in the change ticket adequate?). ISA 530 requires that every
  sampling unit has a chance of selection (para. 8) and that the sample size
  reduces sampling risk to an acceptably low level (para. 7).
- **Regulation mapping**: every rule points to the provision it tests, with a
  verbatim quote that a test checks against the stored official text.

## 1. Time: "held at the time" instead of "held on `as_of`"

Module A tests the state on `as_of` = 2026-09-30. The ledger covers fiscal
year 2025, the change data 2025-10-01 to 2026-09-30. A cross-module rule must
ask whether a right was held **when the action happened**, not today.

Definitions used by every X rule:

- **Action:** one recorded act by one account or employee, with a timestamp.
  The action types are listed in the table below.
- **Action day:** the date part of the action timestamp.
- **Account holds function F at day d:** the account exists (`created_at` date
  `<= d`) and has an assignment with `valid_from <= d` and (`valid_to` empty
  or `valid_to >= d`) to a role that contains an entitlement with
  `business_function = F`, directly or through the role hierarchy (same
  traversal as A-05).
- **Employee of an action:** for account-based actions, the `employee_id` of
  the account in `accounts.csv`; for employee-based actions, the column given
  in the table. Technical and generic accounts have no employee.
- Account `status` has no history in the data (only the state on `as_of`), so
  no X rule uses it.

| Action type | Source | Account / employee | Timestamp | Required function |
|---|---|---|---|---|
| `je_prepare` | `journal_entries.csv`, `source = manual` | `preparer_account_id` | `entry_ts` | `GL_POST` |
| `je_approve` | `journal_entries.csv`, `approver_account_id` set (any source) | `approver_account_id` | `approved_ts` | `GL_APPROVE` |
| `prod_deploy` | `deployments.csv`, `environment = prod` | deploying employee (module C, section 2) | `deployed_ts` | none (D1) |
| `commit` | `commits.csv` | `author_employee_id` | `committed_ts` | none |
| `change_approval` | `change_approvals.csv` | `approver_employee_id` | `decided_ts` | none |
| `recert_decision` | `recert_decisions.csv` | `reviewer_employee_id` | `decided_at` | none |

Automatic journal entries run under technical accounts and are not actions of
a person.

## 2. Input files

No new files. The X rules read the files of modules A, B and C through their
existing loaders and evidence hashes, all from the same directory. Every X finding lists the hashes of all
files it read.

## 3. Rules

Severity values come from `core.findings.Severity`. The ground-truth key is
`(rule_id, entity)`. Each X finding has `details.related`: the
`(rule_id, entity)` keys of A, B and C findings about the same objects, so the
reader can jump from the risk to its use. `related` is computed from the
rules' own output, not from the ground truth.

### X-01 `sod_exercised`: conflicting functions held and used on one transaction

- **Population:** manual journal entries with an approver.
- **Conflicts in scope:** conflicts of the SoD ruleset whose two functions
  both appear in the action table: today only `SOD-GL-01`
  (`GL_POST` / `GL_APPROVE`). Payments, credit and trading have no
  transaction data; their conflicts stay risks (A-05).
- **Exception:** preparer and approver account belong to the **same
  employee** (same or different account), the preparer account held
  `GL_POST` at the entry day and the approver account held `GL_APPROVE` at
  the approval day.
- **Entity:** `employee_id:conflict_id`, the same format as A-05, so both line
  up. One finding per employee and conflict.
- **Severity:** from the ruleset (`SOD-GL-01`: HIGH), raised to CRITICAL if
  at least one entry is at or above the manual approval limit.
- **Details:** `doc_nos` (sorted), `count`, `first_ts`, `last_ts` (earliest
  and latest `entry_ts`),
  `max_amount`, `via_second_account` (bool), `held_on_as_of` (bool, i.e.
  A-05 also reports it), `related` (A-05 with the same entity, B-08 on the
  listed entries).
- Unlike B-08, X-01 also covers entries **below** the approval limit: an
  employee who can post and approve may self-approve small amounts, and the
  four-eyes rule does not catch it.

### X-02 `action_without_entitlement`: action not covered by the access data

- **Population:** actions with a required function: `je_prepare` and
  `je_approve`. Deployments are not tested (D1).
- **Exception:** the acting account did not hold the required function at
  the action day, or the account did not exist yet.
- **Entity:** `doc_no`. One finding per entry; `details.actions` lists `je_prepare`,
  `je_approve` or both.
- **Severity:** HIGH.
- **Details:** `actions`, `checks` (per action: account, function, `ts`,
  reason `account_unknown`, `account_not_created` or `function_not_held`),
  `related` (B findings on the same `doc_no`, A findings on the acting
  account).
- **Why it matters:** either the application does not enforce its
  authorisation model, or the access extract the audit relies on is
  incomplete. Both undermine every A result. The finding says so and does not
  guess which one applies.

### X-03 `activity_after_exit`: a leaver acted after the exit date

- **Population:** all actions of the action table with an employee, over
  the whole file, not only the audit period. Action ids: `doc_no`,
  `deployment_id`, `commit_sha`, `change_id`,
  `campaign_id:account_id:role_id`.
- **Exception:** the employee has an `exit_date` in HR and the action day is
  **after** it. An action on the exit date itself is not flagged.
- **Entity:** `employee_id`. One finding per employee.
- **Severity:** CRITICAL.
- **Details:** `exit_date`, `actions` (list of `type`, `id`, `ts`, sorted by
  `ts`), `days_after_exit_max`, `related` (A-01 accounts of the employee).
- A-01 reports the open door (account still active), X-03 reports that
  someone walked through it.

## 4. Sampling (`sampling.py`)

### 4.1 Sample size for attribute tests

`sample_size(confidence, tolerable_rate, expected_rate, method="binomial",
population=None) -> int`

- Smallest `n` such that, if the sample shows `k = ceil(n * expected_rate)`
  deviations, the auditor can still conclude at `confidence` that the
  population rate does not exceed `tolerable_rate`:
  `P(X <= k) <= 1 - confidence`, with `X ~ Binomial(n, tolerable_rate)`.
- `method="hypergeometric"` uses the known population size `N`:
  `X ~ Hypergeometric(N, ceil(tolerable_rate * N), n)`. Smaller sizes for small
  populations, for example the 90 emergency changes.
- Checks: `0 <= expected_rate < tolerable_rate < 1`, `0 < confidence < 1`. If
  `n >= N`, the result is `N` and the output says "test the full population".
- Reference values (computed for this spec, they become tests):

| Confidence | Tolerable | Expected | Binomial | Hypergeometric, N = 90 |
|---|---|---|---|---|
| 95 % | 5 % | 0 % | 59 | 40 |
| 95 % | 5 % | 1 % | 93 | 59 |
| 95 % | 5 % | 2 % | 181 | 73 |
| 90 % | 5 % | 0 % | 45 | 33 |
| 95 % | 10 % | 0 % | 29 | |

  The first line also has a closed form, `ln(0.05) / ln(0.95) = 58.4`, so 59.
  The test checks both ways.

### 4.2 Evaluation

`evaluate(n, deviations, confidence, tolerable_rate)` returns the one-sided
upper deviation limit (Clopper-Pearson, `beta.ppf(confidence, d + 1, n - d)`)
and whether it stays at or below `tolerable_rate` (ISA 530 para. 15).

### 4.3 Selection

- Named populations, each a filter on one evidence file, sorted by its key:
  `prod_deployments` (C), `changes` (C, in the period), `manual_entries` (B),
  `recert_decisions` (A), `active_personal_accounts` (A).
- A random permutation of the sorted keys from
  `numpy.random.default_rng(seed)`. The first `n` are the sample, the next
  `replacements` (default 5) are replacement items in fixed order, for items
  where the procedure does not apply (ISA 530 para. 10).
- Output: a header with population name, file SHA-256, `N`, all parameters and
  the seed, then one row per item (`rank`, key, `replacement` flag). Same file
  hash, same parameters, same seed: same sample. Property test with
  hypothesis.
- No stratification and no exclusion of rule findings in this phase.

## 5. Regulation mapping (`regulation/`)

- `regulation/mapping.yaml`: rule id -> list of references, each with
  `framework`, `ref`, `quote`, `url`.
- `regulation/sources/`: the verbatim article texts the quotes come from,
  as plain text, with source URL and retrieval date at the top. EU legal texts
  may be reused with attribution (Commission Decision 2011/833/EU).
- Tests: every registered rule has an entry (or an explicit empty list with a
  reason); every quote is a substring of its source file after whitespace
  normalisation; every `url` is the ELI link of the act.
- `Finding.regulation_refs` is filled from the mapping at run time, as
  `"DORA Art. 9(4)(c)"`, `"RTS 2024/1774 Art. 21(b)"`.
- Draft mapping, all provisions read in the EUR-Lex full text on 2026-11-01
  (DORA Art. 8(1), 9(3), 9(4); RTS Art. 17(1), 20, 21):

| Rule | Provision | Quote (short) |
|---|---|---|
| A-01 | RTS Art. 21(e)(iii) | "withdrawal of access rights without undue delay upon termination of the employment" |
| A-02 | RTS Art. 21(e)(iii), Art. 20(2)(b) | "... or when the access is no longer necessary"; "temporary deactivation, and termination of all accounts" |
| A-03 | RTS Art. 21(c) | "limiting to the extent possible the use of generic and shared user accounts" |
| A-04 | RTS Art. 21(e)(ii), (e)(iv) | "assignment of privileged ... access on a need-to-use ... basis"; "update of access rights ... at least once a year" |
| A-05 | RTS Art. 21(b) | "segregation of duties designed to ... prevent the allocation of combinations of access rights that may be used to circumvent controls" |
| A-06 | RTS Art. 21(e)(i), (e)(iv) | "assignment of roles and responsibilities for granting, reviewing, and revoking access rights"; a rubber-stamped review does not meet (e)(iv) |
| A-07 | RTS Art. 20(1), 20(2)(a) | "unique identification and authentication of natural persons"; "a unique identity corresponding to a unique user account" |
| A-08 | RTS Art. 21(a) | "need-to-know, need-to-use and least privilege principles" |
| C-01 to C-06 | DORA Art. 9(4)(e), RTS Art. 17(1) | as in the module C spec, section 0 |
| X-01 | RTS Art. 21(b) | as A-05 |
| X-02 | DORA Art. 9(4)(c), RTS Art. 21(d) | "limit the ... logical access ... to what is required for legitimate and approved functions and activities only" |
| X-03 | RTS Art. 21(e)(iii), 21(c) | "users are identifiable for the actions performed in the ICT systems at all times" |
| B-01 to B-10 | ISA 240 (Revised) paras. 48 and 49, GoBD Rz. 40 (B-06) | quotes added only after reading the original texts in the build step (D2) |

  DORA Art. 9(4)(c) is the umbrella for all A rules and is listed for each.
- The RTS Art. 21 subparagraph "For the purposes of point (e)(i) ... retention
  period ... logs" is not cited: as published it refers to point (e)(i)
  (roles for granting access), which does not fit its content.

## 6. Generator changes and ground truth

The X data comes from the existing generator plus a few targeted changes. A
new random generator `default_rng([seed, 4])` is used, so no A, B or C value
shifts beyond the rows named here. The A, B and C ground-truth **keys** stay
identical (proof: clone `main`, generate seeds 7 and 11 on both, compare the
key sets; byte diffs are listed in the PR).

1. **X-01, two cases.**
   - The cross-account GL employee of A-05 (E00460 on seed 7) gets both
     conflicting assignments and the second account from 2024-12-01 instead
     of 2026-07-01. The B-08 self-approvals of this employee are approved with
     the **second** account. Today the generator approves them with the same
     account, although the ground-truth note says "second GL account", and
     the employee held only `GL_VIEW` in 2025. Both are corrected.
   - One same-account `SOD-GL-01` employee of A-05, employed before
     2025-01-01, gets the two assignments from 2024-12-01 and self-approves two
     entries **below** the approval limit (X-01 only, no B-08).
2. **X-02, four cases, no new injection.** The four B-10 entries (Administration
   and Revision staff who hold only `GL_VIEW`) are already actions without
   entitlement. Measured on seed 7: the clean ledger has 0 such entries, the
   injected one exactly these 4 plus the 3 E00460 entries that item 1 fixes.
3. **X-03, two cases.** The two developer leavers of A-01 (AD accounts) each
   author one commit after their exit date, on changes deployed by someone
   else. Their `last_login` moves after the commit, so A-01 reports
   `logged_in_after_exit = true` and the note changes; the key does not.
   Measured on seed 7: 0 actions after exit in clean and injected data today.

Near misses (must not be flagged):

- the four GL SoD conflicts of A-05 that start on 2026-07-01, after the
  ledger year (risk, not exercised: X-01)
- the SoD employee of item 1 approving another clerk's entry (X-01)
- an entry prepared on the first day of the preparer's role assignment (X-02)
- a leaver's commit on the exit date itself (X-03)
- a leaver whose account was used only before the exit date (X-03)

Requirements as in modules A to C: same seed same bytes, independent of
`PYTHONHASHSEED`; clean bank 0 findings for every rule; injected bank exactly
the ground truth.

Injected counts (default seed): X-01 2, X-02 4, X-03 2, total 8.

## 7. CLI

- `awb run` runs A, B, C and X; `--module x` restricts to X.
- `awb explain` for an X finding shows each action with its account, the
  role assignments valid at that day and the path to the function.
- `awb sample DIR --population NAME --confidence 0.95 --tolerable 0.05
  --expected 0.01 --seed N [--method hypergeometric] [--replacements 5]
  [--output FILE]`, and `awb sample-size` with the same parameters without a
  directory.
- `awb check-ground-truth` adds module X to the table. CI job `ground-truth`
  unchanged on seeds 7 and 11.

## 8. Out of scope

- Who may deploy to production (D1). The access data has no `PROD_DEPLOY`
  function. Adding it would require changing the C-02 near miss "developer
  deploys an unrelated change", which today produces 3 manual production
  deployments by developers in the clean bank.
- Six-month review for systems supporting critical or important functions
  (RTS Art. 21(e)(iv)): A-04 checks 365 days, the lower bar. Which systems
  support critical or important functions is the bank's own classification
  (DORA Art. 8(1)). Listed as a known gap in the README (D3).
- Payment, credit and trading transactions: no data, so their SoD conflicts
  stay risks.
- Monetary unit sampling and tests of details (amounts): attribute sampling
  only.
- Workpaper, API and dashboard: phase 5.

## 9. Build order

1. Shared helper "account holds function F at day d", with tests on small
   builders (role hierarchy, validity limits, account creation date).
2. Rules X-01 to X-03 (`crosslink.py`), one test per trigger and near miss.
3. Generator changes (section 6), iterated against the real rules, then the
   key-set proof against `main`.
4. `sampling.py` with the reference values and property tests.
5. `regulation/` with sources, mapping and quote tests.
6. CLI, CI summary, README numbers from a real run.

Subagents one after another, not in parallel. The spec is binding for them.

## 10. Decisions (Eugen, 2026-11-01)

- **D1** No `PROD_DEPLOY` right in phase 4; X-02 covers journal entries only.
- **D2** Module B is mapped to ISA 240 (Revised) and GoBD Rz. 40, read in the
  original before the quotes go in.
- **D3** The six-month review of RTS Art. 21(e)(iv) is listed as a known gap,
  not built.
