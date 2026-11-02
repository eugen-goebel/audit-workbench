# Phase 5: Audit workpaper (DOCX and PDF)

Design spec for the first part of phase 5. Code and tests are built against
this document. If code and spec disagree, the spec is fixed first.

Status: approved 2026-11-02 (decisions in section 10). Dashboard, API and
Docker are not part of this spec (section 9).

## 0. Why

`awb run` lists findings and `awb explain` shows one of them. What an audit
team files at the end is neither: it is a workpaper that a reviewer can read
without the tool. Today that step is manual copy and paste from the terminal,
which loses the link between a finding and the data it came from.

`awb workpaper` writes that document from one run, in DOCX (to edit and
complete) and PDF (to file). Every number in it comes from the run; the parts
that need an auditor's judgment stay visibly open instead of being filled in
by the tool.

## 1. What the documentation standard asks for

The structure follows ISA 230 *Audit Documentation*, read in the IAASB
handbook text (2023-2024 edition, paras. 8 and 9). No audit firm's template is
used.

> 8. The auditor shall prepare audit documentation that is sufficient to
> enable an experienced auditor, having no previous connection with the audit,
> to understand: (a) The nature, timing and extent of the audit procedures
> performed [...]; (b) The results of the audit procedures performed, and the
> audit evidence obtained; and (c) Significant matters arising during the
> audit, the conclusions reached thereon, and significant professional
> judgments made in reaching those conclusions.
>
> 9. In documenting the nature, timing and extent of audit procedures
> performed, the auditor shall record: (a) The identifying characteristics of
> the specific items or matters tested; (b) Who performed the audit work and
> the date such work was completed; and (c) Who reviewed the audit work
> performed and the date and extent of such review.

How each requirement is met:

| ISA 230 | Section of the workpaper | Filled by |
|---|---|---|
| 8(a) nature, timing, extent | 3 Procedures | the run: rule, version, parameters, population, `as_of` |
| 8(b) results and evidence | 4 Results, 5 Findings | the run: counts, finding records, evidence hashes |
| 8(c) significant matters, conclusions, judgments | 7 Significant matters | **the auditor**: one open row per CRITICAL and HIGH finding |
| 9(a) identifying characteristics | 2 Data basis, 5 Findings, 6 Samples | the run: file SHA-256, row counts, entity keys, sample header |
| 9(b) who performed, when | 8 Sign-off | `--preparer` and the run date |
| 9(c) who reviewed, when, extent | 8 Sign-off | **the reviewer**: left blank on purpose |

The tool never writes a reviewer name or a conclusion. A workpaper that looks
reviewed but was not is worse than one with a visible gap.

The quote goes to `regulation/sources/isa_230.txt` and is checked by a test,
like the phase 4 quotes.

## 2. Command

```
awb workpaper DATA_DIR [--output FILE.docx|FILE.pdf] [--as-of DATE]
              [--module a|b|c|x|all] [--min-severity LEVEL]
              [--sample FILE ...] [--preparer NAME] [--title TEXT]
```

- The format follows the suffix of `--output`; default
  `DATA_DIR/workpaper.docx`. Any other suffix is a usage error (exit 2).
- `--sample` takes one or more files written by `awb sample`; each becomes a
  part of section 6. A sample whose population hash does not match the file in
  `DATA_DIR` is an error: the sample was drawn from different data.
- `--preparer` defaults to empty, shown as "not recorded".
- `--module`, `--min-severity` and `--as-of` mean the same as in `awb run`.
  Filtered-out findings are still counted in section 4, with a note that they
  are not listed.

## 3. Sections

1. **Cover.** Title (default "IT audit workpaper"), audited entity "synthetic
   bank (seed N)" when `ground_truth.json` names a seed, otherwise the folder
   name; `as_of`; periods covered (ledger year, change window); tool name and
   version; run date.
2. **Data basis.** One row per input file: name, rows, columns, SHA-256 (full,
   not a prefix). A note that the findings rest on exactly these bytes.
3. **Procedures.** One row per rule that ran: id, version, title, what it
   tests (one sentence, from the rule docstring), parameters, and the
   regulation references from `regulation/mapping.yaml` with their quote.
4. **Results.** Count per rule and severity, total, and how many were hidden
   by `--min-severity`. Rules with zero findings are listed too: "tested, no
   exception" is a result.
5. **Findings.** Most severe first. Per finding: finding id, rule, severity,
   entity, description, evidence files (SHA-256 prefix, full hash in section
   2), regulation references, and the rule-specific details in short form
   (e.g. the SoD access path, the journal entry number and lines).
6. **Samples** (only with `--sample`). Per sample: population, population
   hash, N, n, method, confidence, tolerable and expected rate, seed, the
   selected keys and the replacement keys, with an empty "result" column for
   the auditor.
7. **Significant matters.** One row per CRITICAL and HIGH finding, with empty
   columns "discussed with / when", "conclusion" and "judgment". Section 1
   states that this section is to be completed by the auditor.
8. **Sign-off.** Prepared by (from `--preparer`) and date of the run. Reviewed
   by, date, extent of review: empty.

## 4. Formats

- One document model (sections, paragraphs, tables) built once from the run,
  then rendered twice. DOCX and PDF therefore cannot drift apart in content.
- DOCX with `python-docx`: real Word tables and headings, so the auditor can
  complete sections 6 to 8 in Word.
- PDF with `reportlab`: A4, page numbers, the data hash of the run in the
  footer of every page. No conversion through LibreOffice, so it runs in CI.
- Characters outside the PDF base font (WinAnsi) are replaced and reported in
  a test; the synthetic data and the quotes are checked against that set.

## 5. Reproducibility

- Same data, same options, same tool version: the same document text. The
  run date is the only moving part; `SOURCE_DATE_EPOCH` fixes it, as for
  reproducible builds.
- File bytes are not compared (DOCX is a zip with timestamps); tests compare
  the extracted text.

## 6. Dependencies

`python-docx` and `reportlab` go into an optional extra `workpaper`
(`uv sync --extra workpaper`, `pip install audit-workbench[workpaper]`).
Without them `awb workpaper` stops with a message naming the extra; all other
commands keep working. CI installs the extra.

## 7. Tests

- Document model: every section present, in order, for seed 7; counts in
  section 4 equal the run; every finding id of the run appears in section 5;
  every input hash appears in section 2.
- DOCX opened with `python-docx`, PDF opened with `pypdf` (dev dependency):
  headings of all eight sections found, finding ids found, footer hash found.
  "The file exists" is not a test.
- Sign-off: reviewer fields are empty, section 7 conclusions are empty.
- `--sample` with a sample of other data fails with exit 1.
- Same input twice with `SOURCE_DATE_EPOCH`: identical extracted text.
- ISA 230 quote checked against `regulation/sources/isa_230.txt`.

## 8. Build steps (one PR)

1. ISA 230 source text and quote test.
2. Document model and section builders, tests on seed 7.
3. DOCX renderer, tests.
4. PDF renderer, tests.
5. CLI command, README section with an excerpt of a real generated workpaper
   (seed 7), not a mock-up.

## 9. Not in this spec

- Dashboard (Streamlit) and live demo: next spec, after this one is merged.
- API (FastAPI) and Docker: kept for later (E3), after the dashboard.
- German-language workpaper: English only, like the rest of the repo.

## 10. Decisions (Eugen, 2026-11-02)

- **E1** Reviewer fields and conclusions stay empty; section 1 (cover) says
  the auditor completes them.
- **E2** `python-docx` and `reportlab` are an optional extra `workpaper`.
- **E3** API and Docker are kept for later, after the dashboard.
