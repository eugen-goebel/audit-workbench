# Module A: Access review and segregation of duties

Design spec for phase 1. Rules, data model and the synthetic bank are built
against this document. If code and spec disagree, the spec is fixed first.

## 1. Audit date

Every run has an audit reference date `as_of` (a `datetime.date`). All "still
active", "older than" and "within the last" checks are relative to `as_of`,
never to the current clock. The synthetic bank uses `as_of = 2026-09-30`.

## 2. Input files

All files are CSV, UTF-8, comma separated, read as text (see
`core.evidence`). Empty string means "no value". Dates are ISO `YYYY-MM-DD`,
timestamps ISO `YYYY-MM-DDTHH:MM:SS` (no timezone, bank local time).
Booleans are `true` / `false`.

| Evidence name | File | Columns |
|---|---|---|
| `hr_employees` | `hr_employees.csv` | `employee_id, name, org_unit, job_function, entry_date, exit_date` |
| `accounts` | `accounts.csv` | `account_id, system, employee_id, account_type, interactive, status, created_at, last_login` |
| `roles` | `roles.csv` | `role_id, system, name, privileged` |
| `role_hierarchy` | `role_hierarchy.csv` | `parent_role_id, child_role_id` |
| `entitlements` | `entitlements.csv` | `entitlement_id, system, name, business_function` |
| `role_entitlements` | `role_entitlements.csv` | `role_id, entitlement_id` |
| `account_roles` | `account_roles.csv` | `account_id, role_id, valid_from, valid_to` |
| `recert_campaigns` | `recert_campaigns.csv` | `campaign_id, system, start_date, end_date` |
| `recert_decisions` | `recert_decisions.csv` | `campaign_id, account_id, role_id, reviewer_employee_id, decision, item_shown_at, decided_at` |

Value sets:

- `account_type`: `personal` (bound to one employee), `technical` (service account, `employee_id` empty), `generic` (shared, `employee_id` empty).
- `status`: `active`, `disabled`.
- `decision`: `keep`, `revoke`.
- `last_login`: timestamp or empty (never logged in).
- `exit_date`: date or empty (still employed). An employee with `exit_date < as_of` has left.
- `valid_to`: date or empty (open-ended). An assignment is **active on `as_of`** if `valid_from <= as_of` and (`valid_to` empty or `valid_to >= as_of`).
- `role_hierarchy`: a parent role contains all entitlements of its child roles, transitively. Cycles are a data error: rules must not loop forever, and the loader reports them.

Systems in the synthetic bank: `CORE` (core banking), `PAY` (payments), `GL` (general ledger), `TRADE` (trading), `AD` (directory).

## 3. Business functions and the SoD ruleset

Entitlements carry a `business_function` (for example `PAYMENT_APPROVE`).
Segregation-of-duties conflicts are defined between functions, never between
people or role names, in `src/audit_workbench/access/sod_ruleset.yaml`:

```yaml
conflicts:
  - id: SOD-PAY-01
    functions: [PAYMENT_CREATE, PAYMENT_APPROVE]
    severity: critical
    description: Create and approve the same payment
```

A function is held by an **employee**, not by an account. An employee who can
create payments with their PAY account and approve them with a second PAY
account has the conflict. This cross-account view is deliberate.

## 4. Rules

Severity values come from `core.findings.Severity`. `entity` is the key used
for the ground-truth comparison and must follow the format given here exactly.

### A-01 `leaver_active`: account active after exit

- **Population:** accounts with `account_type = personal` and `status = active`.
- **Exception:** the employee exists in HR, has an `exit_date`, and `exit_date + grace_days < as_of`.
- **Params:** `as_of`, `grace_days` (default 0).
- **Entity:** `account_id`. **Severity:** HIGH.
- **Details:** `employee_id`, `exit_date`, `days_since_exit`, `last_login`, `logged_in_after_exit` (bool).

### A-02 `dormant_account`: active but unused

- **Population:** accounts with `status = active` and `interactive = true`, excluding accounts that are A-01 exceptions (a leaver is the stronger finding).
- **Exception:** `last_login` is older than `as_of - dormant_days`, or `last_login` is empty and `created_at` is older than `as_of - dormant_days`.
- **Params:** `as_of`, `dormant_days` (default 90).
- **Entity:** `account_id`. **Severity:** MEDIUM.

### A-03 `generic_interactive`: shared account with interactive login

- **Exception:** `account_type = generic`, `interactive = true`, `status = active`.
- **Entity:** `account_id`. **Severity:** MEDIUM.

### A-04 `privileged_unreviewed`: privileged access without recertification

- **Population:** assignments active on `as_of`, on accounts with `status = active`, where the assigned role is privileged **or contains a privileged role through the hierarchy**.
- **Exception:** no `recert_decisions` row for this `account_id` and `role_id` in a campaign of the same system whose `end_date` lies within `[as_of - recert_days, as_of]`.
- **Params:** `as_of`, `recert_days` (default 365).
- **Entity:** `account_id:role_id`. **Severity:** HIGH.

### A-05 `sod_conflict`: conflicting functions held by one employee

- **Population:** per employee, all accounts with `status = active` and `account_type = personal`, all their assignments active on `as_of`, every role reachable through the hierarchy, every entitlement of those roles.
- **Exception:** the employee holds both functions of a conflict in the ruleset.
- **Params:** `as_of`, `ruleset_path` (default the bundled ruleset).
- **Entity:** `employee_id:conflict_id`. **Severity:** from the ruleset.
- **Details:** for each of the two functions one access path, as a list of node ids from account to entitlement, for example `["PAY-0042", "R-PAY-TEAMLEAD", "R-PAY-RELEASE", "E-PAY-APPROVE"]`. Shortest path, ties broken by sorting, so the output is deterministic.

### A-06 `recert_rubber_stamp`: recertification not performed with care

One finding per campaign and reviewer, if at least one trigger applies:

- **Self-review:** the reviewer decided `keep` on an account that belongs to themselves.
- **Speed:** the reviewer made at least `min_items` decisions and at least `fast_share` of them took less than `fast_seconds` (`decided_at - item_shown_at`).

- **Params:** `min_items` (default 20), `fast_seconds` (default 5), `fast_share` (default 0.8).
- **Entity:** `campaign_id:reviewer_employee_id`. **Severity:** HIGH if self-review, else MEDIUM.
- **Details:** `triggers` (list of `self_review`, `speed`), `decisions`, `fast_decisions`, `self_reviewed_accounts`.

### A-07 `orphan_account`: personal account without an employee

- **Exception:** `account_type = personal`, `status = active`, and `employee_id` is empty or not in HR.
- **Entity:** `account_id`. **Severity:** HIGH.

### A-08 `unusual_role`: role rarely held by peers

- **Population:** assignments active on `as_of` on active personal accounts of employees who have not left.
- **Peer group:** employees who have not left, with the same `org_unit` and `job_function`. Only groups with at least `min_peers` members are assessed.
- **Exception:** fewer than `rare_share` of the peer group hold the role (on any account, as a direct assignment).
- **Params:** `as_of`, `min_peers` (default 5), `rare_share` (default 0.1).
- **Entity:** `account_id:role_id`. **Severity:** LOW.
- **Details:** `peer_group_size`, `peers_with_role`.

## 5. Synthetic bank and ground truth

`audit_workbench.synth.generate_bank(seed, out_dir)` writes all files from
section 2 and `ground_truth.json`:

```json
{"as_of": "2026-09-30", "seed": 7, "issues": [{"rule_id": "A-01", "entity": "PAY-0042", "note": "..."}]}
```

Requirements:

1. Same seed, same bytes. Generation must be fully deterministic.
2. Without injection the bank is **clean**: every rule returns zero findings. A test checks this.
3. Injection adds known issues and records each in `ground_truth.json`. The run on the injected bank must produce **exactly** the ground-truth set of `(rule_id, entity)`, no more, no fewer.
4. The bank contains **near misses** that must not be flagged, so rules cannot pass by being too broad. At least: a leaver whose account is disabled, a leaver inside the grace period, an account last used 89 days before `as_of`, a non-interactive generic account, a technical account that never logged in, a privileged role reviewed in a campaign that ended 300 days ago, an employee with two functions that do not conflict, a reviewer with many decisions that are slow enough, a rare role in a peer group below `min_peers`.
5. Volumes: about 800 employees, 6 org units, 5 systems, 3 levels of role inheritance at least once, one recertification campaign per system in the 12 months before `as_of`.

Injected issue counts (default seed):

| Rule | Count | Notes |
|---|---|---|
| A-01 | 12 | 4 of them logged in after the exit date |
| A-02 | 10 | |
| A-03 | 3 | |
| A-04 | 6 | 2 of them privileged only through inheritance |
| A-05 | 9 | 3 only through 3-level inheritance, 2 across two accounts of the same employee |
| A-06 | 3 | 1 self-review, 2 speed |
| A-07 | 4 | |
| A-08 | 8 | |
