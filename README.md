# Audit Workbench

[![CI](https://github.com/eugen-goebel/audit-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/eugen-goebel/audit-workbench/actions/workflows/ci.yml)
[![CodeQL](https://github.com/eugen-goebel/audit-workbench/actions/workflows/codeql.yml/badge.svg)](https://github.com/eugen-goebel/audit-workbench/actions/workflows/codeql.yml)
![Python](https://img.shields.io/badge/Python-3.12+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

Data-driven IT audit tests for banks: access review, segregation of duties, journal entry testing and change management. Everything runs on a synthetic bank with known, injected issues, so every detection rule can be checked against ground truth.

> **Status:** module A (access review and segregation of duties) and module B (journal entry testing) are complete. Change management and the cross-module view follow phase by phase.

## Scope and boundaries

- **Synthetic data only.** All data comes from the built-in generator. No real or anonymised data from any bank, client or employer.
- **Public sources only.** Rules are based on publicly available material: Regulation (EU) 2022/2554 (DORA), BaFin publications and published statistical methods. No internal methodology of any audit firm.
- **DORA first.** DORA has applied since 17 January 2025. BaFin repeals the BAIT completely with effect from 31 December 2026, so BAIT appears only as a legacy reference.

## Quick start

```bash
uv sync
uv run awb generate data/generated/demo --seed 7   # build the synthetic bank
uv run awb run data/generated/demo                 # run the access and journal rules
uv run awb run data/generated/demo --module b      # journal entries only
uv run awb explain data/generated/demo <finding-id>
uv run awb benford data/generated/demo --by employee   # digit tables behind B-01/B-02
uv run awb check-ground-truth data/generated/demo  # compare against the injected issues
```

## Module A: access review and segregation of duties

Eight rules, specified in [docs/design/module-a-access.md](docs/design/module-a-access.md):

| Rule | What it finds |
|---|---|
| A-01 | Account still active after the employee left |
| A-02 | Interactive account unused for more than 90 days |
| A-03 | Shared (generic) account that allows interactive login |
| A-04 | Privileged access without recertification in the last 12 months, including privilege inherited through nested roles |
| A-05 | One employee holds two conflicting business functions, across all of their accounts and through nested roles |
| A-06 | Recertification done without care: reviewers approving their own access, or clicking through most items in under five seconds |
| A-07 | Personal account without a matching HR record |
| A-08 | Role that fewer than 10 % of the employee's peer group holds |

## Module B: journal entry testing

Ten rules on the general ledger, specified in [docs/design/module-b-journal.md](docs/design/module-b-journal.md). The population follows ISA 240 (Revised) paragraphs 48 and 49: completeness of the journal entries, entries at period end and entries throughout the period.

| Rule | What it finds |
|---|---|
| B-01 | A preparer's manual amounts do not follow Benford's law in the first digit (MAD above 0.015) |
| B-02 | A GL account's amounts do not follow Benford's law in the first two digits (MAD above 0.0022) |
| B-03 | Share of round amounts per preparer above 25 % (at least 30 entries) |
| B-04 | Manual entry on a weekend or a Hessen public holiday |
| B-05 | Manual entry after the period was closed, or reversed within 10 days |
| B-06 | Gaps or duplicates in the document number ranges (GoBD completeness) |
| B-07 | The same entry posted twice within 7 days (same amount, accounts, preparer and text) |
| B-08 | Manual entry above the approval limit without a second, different approver |
| B-09 | Rare debit/credit account combination (fewer than 3 entries per year on an income, equity or suspense account) |
| B-10 | Manual entry keyed by someone outside the accounting department or by a system administrator |

The Benford rules judge a group only above a minimum size, because the MAD of even perfectly Benford-conforming amounts grows as the sample shrinks. A simulation in the test suite shows why: at 300 amounts, more than 20 % of clean samples exceed the 0.015 limit; at 1,000, none do. `awb benford` lists every group, including the ones too small to judge.

The anomaly ranking (`awb run --rank N`) scores manual entries with an Isolation Forest. It is a sampling aid for choosing entries to inspect and deliberately produces no findings: a score without an explainable threshold cannot be defended in an audit.

### The synthetic bank (both modules)

`awb generate` builds a fictitious bank from a seed: 821 employees in seven org units, 851 accounts on five systems (core banking, payments, general ledger, trading, directory), roles nested up to three levels deep, and one recertification campaign per system. Module B adds a chart of accounts and a fiscal year 2025 with about 60,000 journal entries (about 20 % manual) and 180,000 lines. It then injects known issues and writes them to `ground_truth.json`.

The clean bank, without injection, produces no findings in either module. The bank also contains near misses that must not be flagged, for example a leaver whose account is disabled, an account last used 89 days ago, a rare role in a team too small to compare, entries on 24 and 31 December (normal banking days), an amount one cent below the approval limit, and a small group whose Benford MAD looks high purely by chance.

### Ground-truth result

Output of `awb check-ground-truth` on the default seed. CI runs the same check on seeds 7 and 11 for every push and publishes the table in the job summary.

| module | rule | title | expected | found | true positives | recall | precision |
|---|---|---|---|---|---|---|---|
| A | A-01 | Account active after employee exit | 12 | 12 | 12 | 100.0% | 100.0% |
| A | A-02 | Dormant interactive account | 10 | 10 | 10 | 100.0% | 100.0% |
| A | A-03 | Generic account with interactive login | 3 | 3 | 3 | 100.0% | 100.0% |
| A | A-04 | Privileged access not recertified | 6 | 6 | 6 | 100.0% | 100.0% |
| A | A-05 | Segregation-of-duties conflict | 9 | 9 | 9 | 100.0% | 100.0% |
| A | A-06 | Recertification rubber stamp | 3 | 3 | 3 | 100.0% | 100.0% |
| A | A-07 | Orphan personal account | 4 | 4 | 4 | 100.0% | 100.0% |
| A | A-08 | Unusual role for peer group | 8 | 8 | 8 | 100.0% | 100.0% |
| B | B-01 | Benford first-digit test per preparer | 1 | 1 | 1 | 100.0% | 100.0% |
| B | B-02 | Benford first-two-digits test per GL account | 1 | 1 | 1 | 100.0% | 100.0% |
| B | B-03 | High share of round amounts per preparer | 2 | 2 | 2 | 100.0% | 100.0% |
| B | B-04 | Manual entry on weekend or public holiday | 6 | 6 | 6 | 100.0% | 100.0% |
| B | B-05 | Post-closing entries and early reversals | 8 | 8 | 8 | 100.0% | 100.0% |
| B | B-06 | Gaps and duplicates in document numbers | 4 | 4 | 4 | 100.0% | 100.0% |
| B | B-07 | Same entry posted twice | 5 | 5 | 5 | 100.0% | 100.0% |
| B | B-08 | Manual entry above the limit not approved by a second person | 6 | 6 | 6 | 100.0% | 100.0% |
| B | B-09 | Rare debit/credit account combination | 5 | 5 | 5 | 100.0% | 100.0% |
| B | B-10 | Manual entry by an unexpected preparer | 4 | 4 | 4 | 100.0% | 100.0% |
| A | **Total A** |  | 55 | 55 | 55 | 100.0% | 100.0% |
| B | **Total B** |  | 42 | 42 | 42 | 100.0% | 100.0% |
|  | **Total** |  | 97 | 97 | 97 | 100.0% | 100.0% |

This shows the rules match their specification on data built to that specification. It says nothing about real-world data quality, which is always messier.

### Explaining a segregation-of-duties conflict

A conflict is only useful if the auditor can see where the access comes from. `awb explain` prints the shortest path from the account to each entitlement:

```text
Finding:     a4cf4ee1ffdddb87
Rule:        A-05 v1.0.0 - Segregation-of-duties conflict
Severity:    HIGH
Entity:      E00461:SOD-GL-01
Description: Employee E00461 holds both GL_POST and GL_APPROVE (Post and approve manual journal entries).
Audit date:  2026-09-30

Access paths:
  GL_POST: GL-00133 -> R-GL-MANAGER -> R-GL-TEAMLEAD -> R-GL-CLERK -> E-GL-POST
  GL_APPROVE: GL-00133 -> R-GL-APPROVER -> E-GL-APPROVE
```

The posting right is not in the assigned role itself. It comes from `R-GL-CLERK`, two levels down (manager contains team lead contains clerk). A review that only looks at directly assigned roles would miss it.

## Design principles

**Reproducible evidence.** Every input file is hashed with SHA-256 when it is loaded. Each finding stores the rule id, the rule version, its parameters (including the audit date) and the hashes of the data it came from. Rerunning a rule on the same data gives the same finding id.

**Rules are versioned.** A rule is one audit test with a stable id (for example `A-01`) and a version. Changing the logic means a new version, so old results stay explainable.

**Fixed audit date.** Every check is relative to the audit date (`as_of`), never to the current clock, so results do not change when the same data is rerun a week later.

## Roadmap

| Module | Audit area | Status |
|---|---|---|
| Core | Evidence loading and hashing, finding model, rule registry | done |
| A | Access review and segregation of duties | done |
| B | Journal entry testing | done |
| C | Change management | planned |
| D | Cross-module findings, sampling, DORA mapping | planned |
| E | Audit workpaper (DOCX/PDF), API, dashboard | planned |

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Auf Deutsch

IT-Prüfungshandlungen für Banken, datenbasiert umgesetzt. Vier Bereiche werden abgedeckt: Berechtigungsprüfung, Funktionstrennung, Journal-Entry-Testing und Change-Management. Getestet wird an einer synthetischen Bank, deren Fehler bekannt sind und absichtlich eingebaut wurden. Dadurch lässt sich jede Prüfregel gegen ein bekanntes Ergebnis prüfen.

Modul A ist fertig, die Berechtigungsprüfung mit acht Regeln. Modul B ebenfalls, das Journal-Entry-Testing mit zehn Regeln, darunter Benford-Analysen, Buchungen am Periodenende, Lücken in Belegnummern, Dubletten und fehlende Vier-Augen-Freigabe. Sie findet unter anderem aktive Konten ausgeschiedener Mitarbeiter, Privilegien ohne Rezertifizierung und Verstöße gegen die Funktionstrennung. Auch Verstöße, die erst über mehrere Ebenen vererbter Rollen entstehen, werden erkannt. Zu jedem Fund zeigt das Werkzeug den Weg vom Konto bis zur einzelnen Berechtigung.

Alle Daten sind künstlich erzeugt. Echte Daten kommen nicht vor. Die Prüfregeln beruhen nur auf öffentlichen Quellen, nicht auf der Methodik einer Prüfungsgesellschaft.

## License

MIT
