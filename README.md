# Audit Workbench

[![CI](https://github.com/eugen-goebel/audit-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/eugen-goebel/audit-workbench/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

Data-driven IT audit tests for banks: access review, segregation of duties, journal entry testing and change management. Everything runs on a synthetic bank with known, injected issues, so every detection rule can be checked against ground truth.

> **Status:** early development. The foundation (evidence handling, finding model, rule registry) is in place. The audit modules follow phase by phase.

## Scope and boundaries

- **Synthetic data only.** All data comes from the built-in generator. No real or anonymised data from any bank, client or employer.
- **Public sources only.** Rules are based on publicly available material: Regulation (EU) 2022/2554 (DORA), BaFin publications and published statistical methods. No internal methodology of any audit firm.
- **DORA first.** DORA has applied since 17 January 2025. BaFin repeals the BAIT completely with effect from 31 December 2026, so BAIT appears only as a legacy reference.

## Design principles

**Reproducible evidence.** Every input file is hashed with SHA-256 when it is loaded. Each finding stores the rule id, the rule version, its parameters and the hashes of the data it came from. Rerunning a rule on the same data gives the same finding id.

**Rules are versioned.** A rule is one audit test with a stable id (for example `A-01`) and a version. Changing the logic means a new version, so old results stay explainable.

## Planned modules

| Module | Audit area | Status |
|---|---|---|
| Core | Evidence loading and hashing, finding model, rule registry | done |
| A | Access review and segregation of duties | planned |
| B | Journal entry testing | planned |
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

IT-Prüfungshandlungen für Banken, gestützt auf Daten. Vier Bereiche stehen im Fokus: Berechtigungsprüfung, Funktionstrennung, Journal-Entry-Testing und Change-Management. Getestet wird an einer synthetischen Bank, deren Fehler bekannt und absichtlich eingebaut sind. So lässt sich jede Prüfregel gegen ein bekanntes Ergebnis prüfen.

Alle Daten sind erzeugt, echte Daten kommen nicht vor. Die Prüfregeln stützen sich nur auf öffentliche Quellen, nicht auf die Methodik einer Prüfungsgesellschaft.

## License

MIT
