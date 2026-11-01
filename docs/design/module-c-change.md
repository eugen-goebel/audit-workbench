# Module C: Change management

Design spec for phase 3. Rules, data model and the synthetic change data are
built against this document. If code and spec disagree, the spec is fixed
first.

Status: approved 2026-11-01.

## 0. Why these tests

DORA (Regulation (EU) 2022/2554) Article 9(4), point (e), requires documented
policies, procedures and controls for ICT change management "in order to
ensure that all changes to ICT systems are recorded, tested, assessed,
approved, implemented and verified in a controlled manner".

Commission Delegated Regulation (EU) 2024/1774 (RTS on ICT risk management),
Article 17(1), lists what those procedures must contain. This module tests the
parts that leave a trace in data:

| RTS Art. 17(1) | Requirement | Rules |
|---|---|---|
| (b) | independence of the functions that approve changes from those that request and implement them | C-02, C-05 |
| (c)(iii) | changes are tested and finalised in a controlled manner | C-06 |
| (f) | emergency changes with adequate safeguards | C-03 |
| (g) | emergency changes documented, re-evaluated, assessed and approved after implementation | C-03 |
| DORA 9(4)(e) | all changes recorded and approved | C-01, C-04 |

Both texts were read in the official EUR-Lex version. BAIT is not cited:
BaFin repeals it with effect from 31 December 2026.

The module tests what the records show. It does not judge whether a test was
adequate or a risk assessment sound; that stays with the auditor.

## 1. Audit period and dates

- `as_of` = **2026-09-30**, the same audit date as module A.
- Audit period = the 12 months up to `as_of`: **2025-10-01 to 2026-09-30**.
  The population is every **production** deployment with `deployed_ts` in the
  period, and every change that has at least one of those deployments.
- Timestamps are bank local time without timezone, as in modules A and B.
- **First production deployment** of a change: the earliest `prod` deployment
  in the period linked to it, whatever its `result`. A failed attempt has already touched
  production, so C-03, C-04 and C-06 all measure from it.
- People are identified as **employees**, not accounts. A deployment with the
  second AD account of the developer is still a self-deployment.

## 2. Input files

Same conventions as modules A and B: CSV, UTF-8, read as text, empty string =
no value, ISO dates and timestamps.

| Evidence name | File | Columns |
|---|---|---|
| `changes` | `changes.csv` | `change_id, system, change_type, risk, title, requester_employee_id, created_ts, test_signoff_ts, tester_employee_id, status` |
| `change_approvals` | `change_approvals.csv` | `change_id, approval_stage, approver_employee_id, decision, decided_ts` |
| `commits` | `commits.csv` | `commit_sha, system, author_employee_id, committed_ts, change_id` |
| `deployments` | `deployments.csv` | `deployment_id, system, environment, deployed_ts, account_id, method, released_by_employee_id, change_id, result` |

Module C also reads `hr_employees.csv` and `accounts.csv` from module A to
resolve accounts to employees and to check org units.

Value sets:

- `system`: `CORE`, `PAY`, `GL`, `TRADE`, `AD`, as in module A.
- `change_type`: `standard` (pre-approved routine change from a catalogue,
  needs no individual approval), `normal`, `emergency`.
- `risk`: `low`, `medium`, `high`.
- `status`: `closed`, `cancelled`, `in_progress`.
- `approval_stage`: `pre` (before implementation) or `post` (after an
  emergency change).
- `decision`: `approved`, `rejected`.
- `environment`: `test`, `prod`. Only `prod` is tested; test deployments by
  developers are normal.
- `method`: `pipeline` (CI/CD, `account_id` is a technical account,
  `released_by_employee_id` is the person who released it) or `manual`
  (`account_id` is a personal account, `released_by_employee_id` empty).
- `result`: `success`, `failed`, `rolled_back`.
- `change_id` in `deployments` and `commits`: empty if none was given.
- `test_signoff_ts`, `tester_employee_id`: empty if there is no test sign-off.

**Deploying employee** of a production deployment: for `manual`, the employee
of `account_id` in `accounts.csv`; for `pipeline`, `released_by_employee_id`.

**Effective approval** of a change: the latest `pre` decision with
`decision = approved`, provided no `rejected` decision came after it. A
rejection followed by a later approval counts as approved.

The loader reports as data errors (not as findings): unknown `account_id`,
a `manual` deployment with a technical account, a `pipeline` deployment
without `released_by_employee_id`, unknown values in the value sets.

## 3. Rules

Severity values come from `core.findings.Severity`. The ground-truth key is
`(rule_id, entity)`. Population for all rules: production deployments in the
audit period and their changes, as defined in section 1.

### C-01 `deploy_without_change`: production deployment without an approved change

One finding per deployment, if at least one trigger applies:

- `missing`: `change_id` is empty.
- `unknown`: `change_id` does not exist in `changes.csv`.
- `system_mismatch`: the change belongs to a different system than the
  deployment (a ticket reused for an unrelated system).
- `not_approved`: a `normal` change without an effective approval at all, or a
  change with status `cancelled`.

`standard` changes need no approval. Emergency changes without pre-approval
are covered by C-03, not here.

- **Entity:** `deployment_id`. **Severity:** HIGH.
- **Details:** `triggers`, `deploying_employee_id`, `change_id`.

### C-02 `self_deploy`: developer deployed their own change

- **Exception:** the deploying employee is the author of at least one commit
  linked to the same change.
- Applies to `manual` and `pipeline` deployments alike. For a pipeline, the
  release is the control point, so a developer releasing their own change is a
  self-deployment even though a technical account ran it.
- **Entity:** `deployment_id`. **Severity:** HIGH.
- **Details:** `deploying_employee_id`, `account_id`, `method`,
  `commit_shas` (sorted), `via_second_account` (bool: manual deployment with
  an account other than the employee's primary AD account, which is the
  personal AD account with the earliest `created_at`, then `account_id`).

### C-03 `emergency_post_approval`: emergency change not approved afterwards

- **Population:** `emergency` changes with at least one production deployment
  in the period.
- **Exception, `missing`:** no `post` approval with `decision = approved`.
- **Exception, `late`:** the first approved `post` decision is more than
  `post_days` calendar days after the first production deployment.
- **Params:** `post_days` (default 7). The RTS sets no deadline; seven days is
  a parameter the auditor sets from the bank's own policy, and the finding
  states it.
- **Entity:** `change_id`. **Severity:** HIGH for `missing`, MEDIUM for `late`.
- An emergency change still waiting for post-approval at `as_of` is only
  flagged once `post_days` have passed since the deployment.

### C-04 `approval_after_deploy`: approval came after the deployment

- **Population:** `normal` changes with an effective approval.
- **Exception:** no approval was in force at the first production deployment
  of the change (any result): the latest `pre` decision with
  `decided_ts <= deployed_ts` is missing or a rejection. Equal timestamps are
  not flagged. A re-approval after a deployment that was already approved is
  not flagged either.
- **Entity:** `change_id`. **Severity:** HIGH.
- **Details:** `first_deployment_id`, `deployed_ts`, `approved_ts`,
  `hours_before_approval`.

### C-05 `approver_not_independent`: approver requested or built the change

- **Population:** every `approved` decision (`pre` or `post`) on a change in
  the population.
- **Exception:** the approver is the requester of the change, or the author of
  a commit linked to the change (RTS Art. 17(1)(b)).
- **Entity:** `change_id`. **Severity:** HIGH.
- **Details:** `approver_employee_id`, `approval_stage`, `role`
  (`requester`, `author` or both).
- One finding per change, even if several approvals are affected.

### C-06 `untested_change`: deployed without test sign-off

- **Population:** `normal` changes with risk `medium` or `high` (standard and
  emergency changes are out of scope; emergency changes are re-evaluated
  under C-03).
- **Exception:** `test_signoff_ts` is empty, or it is after the first
  production deployment, or the tester is an author of a commit
  linked to the change.
- **Entity:** `change_id`. **Severity:** MEDIUM.
- **Details:** `trigger` (`missing`, `after_deploy`, `self_tested`).

## 4. Synthetic change data and ground truth

`generate_bank(...)` additionally writes the files from section 2 and appends
the issues to `ground_truth.json`. Change data is generated last, with its
own random generator derived from the seed, so it does not shift any A or B
value. Module B files stay byte-identical. Module A files change only by the
rows appended for the second AD account below. A (55/55) and B (42/42) stay
unaffected, and their ground-truth entries stay identical.

Requirements, as in modules A and B:

1. Same seed, same bytes, independent of `PYTHONHASHSEED`.
2. Without injection, every C rule returns zero findings.
3. With injection, the run returns exactly the ground truth.

People, all taken from the existing HR data:

- **Developers:** org unit `Entwicklung`. They author commits and deploy to
  `test`.
- **Operators:** `IT-Betrieb`, job function `Administration`, do manual
  production deployments with their personal AD account.
- **Release managers:** `IT-Betrieb`, `Sachbearbeitung`, release pipeline
  deployments.
- **Approvers (change advisory board):** `IT-Betrieb`, `Teamleitung`.
- **Requesters:** team leads of the business units (`Teamleitung` in
  Zahlungsverkehr, Kredit, Rechnungswesen, Handel).
- **Testers:** `Analyse` in the business unit of the system.
- Pipeline deployments run under one existing technical AD account.
- Only employees active over the whole period take part, so no leaver
  appears in change data (that would mix in module A findings).

Volume (approximate): 1,800 changes in the period (65 % normal, 30 %
standard, 5 % emergency), 6,000 commits, 1,850 production deployments (about
15 % fail or are rolled back and are redeployed), 3,000 test deployments. About 70 % of
production deployments go through the pipeline.

Near misses that must not be flagged:

- a `standard` change deployed without any approval (C-01)
- a change rejected first and approved later, before the deployment (C-01, C-04)
- approval and deployment at the same timestamp (C-04)
- an emergency change post-approved exactly 7 days after deployment (C-03)
- an emergency change deployed 3 days before `as_of`, post-approval still open (C-03)
- a developer deploying their own change to `test` (C-02 is prod only)
- a developer who wrote commits for change X and deploys an unrelated change Y (C-02)
- a pipeline deployment released by a team colleague of the author (C-02)
- a low-risk normal change without test sign-off (C-06 covers medium and high)
- a failed deployment followed by a successful redeployment of the same change
  (once per change for C-03 to C-06; C-01 and C-02 report every deployment,
  so the generator injects their issues on changes with a single deployment)

Injected issue counts (default seed):

| Rule | Count | Notes |
|---|---|---|
| C-01 | 6 | 2 missing, 1 unknown, 1 system mismatch, 2 not approved (one cancelled) |
| C-02 | 5 | 2 manual with the developer's own account, 1 manual with a second AD account of the developer, 2 pipeline released by the author |
| C-03 | 4 | 2 missing, 2 late (one at day 8) |
| C-04 | 4 | |
| C-05 | 4 | 2 approver = requester, 2 approver = commit author |
| C-06 | 3 | 1 missing, 1 after deploy, 1 self-tested |

Total 26. As in module B, some changes carry two characteristics on purpose
(for example a self-deployed change that was also approved by its author).

For the second AD account (C-02), the generator gives one developer an
additional personal AD account with the standard role of their peer group,
a recent login and a recertification decision, so it is clean for every A
rule. The A ground-truth check proves that.

## 5. CLI

- `awb run` runs modules A, B and C. `--module a|b|c` restricts the run.
- `awb check-ground-truth` reports A, B and C in one table, grouped by module.
- `awb explain` shows for a C finding the change, its approvals in time
  order, the linked commits with authors, and each production deployment
  with the deploying employee and the account used.
- CI job `ground-truth` runs unchanged on seeds 7 and 11 and now covers C.

## 6. Out of scope, for phase 4

- Who is *allowed* to deploy to production (a `PROD_DEPLOY` entitlement and a
  SoD conflict with code changes) belongs to module A data. Phase 4 links it:
  a developer who holds the right is a risk, a developer who used it (C-02) is
  an exercised conflict.
- Change freeze windows, fall-back plans (RTS Art. 17(1)(e)) and security
  impact assessments (17(1)(h)) are not in the data model.

## 7. Build order

1. Data loading (`change/data.py`) and test builders, tests first.
2. Rules C-01 to C-06 (`change/rules.py`), one test per trigger and per near miss.
3. Generator (`synth/change.py`) and injection, iterated against the real rules.
4. CLI, ground truth, CI summary, README with numbers from a real run.

Subagents one after another, not in parallel. The spec is binding for them.
