# Phase 5: API and Docker

Design spec for the last part of phase 5. Code and tests are built against this
document. If code and spec disagree, the spec is fixed first.

Status: draft 2026-11-04, waiting for approval.

## 0. Why

The CLI, the workpaper and the dashboard all start from one `Run`. What is
missing is a way for another program to ask for the same results: a GRC tool
that pulls findings into its issue list, a notebook that compares two seeds, a
CI job of a bank that fails when a CRITICAL finding appears. A small read-only
HTTP API answers that. Docker makes the API and the dashboard start with one
command on any machine, without a local Python setup.

Both are thin: they add no rule, no data and no new numbers. Every response must
equal what `awb run --format json` already prints for the same data.

## 1. API surface

FastAPI app `audit_workbench.api`, read-only, JSON unless noted. OpenAPI docs at
`/docs` come with FastAPI.

| Method and path | Returns |
|---|---|
| `GET /health` | `{"status": "ok", "version": ...}` |
| `POST /runs` | starts or reuses a run; body `{"seed": 7}` or `{"data_dir": "name"}`, optional `as_of`, `module` |
| `GET /runs/{run_id}` | summary: data basis with full SHA-256, KPIs, counts per module and severity, evidence quality when ground truth exists |
| `GET /runs/{run_id}/findings` | finding records as in `awb run --format json`; filters `module`, `severity` (minimum), `rule`; paging `limit` (default 100, max 500) and `offset` |
| `GET /runs/{run_id}/findings/{finding_id}` | one finding with the detail the dashboard shows: what the rule tests, parameters, regulation references with quote, evidence files, rule-specific detail, raw rows |
| `GET /runs/{run_id}/ground-truth` | the comparison `awb check-ground-truth` prints, as JSON (404 without `ground_truth.json`) |
| `GET /runs/{run_id}/workpaper?format=docx\|pdf` | the workpaper file; 501 when the `workpaper` extra is not installed |

- **run_id** is derived, not random: SHA-256 over the data hash (the workpaper
  definition: sorted `<file> <sha256>` lines) plus `as_of` and `module`, first
  16 hex characters. The same data and options always give the same id, so a
  caller can store it and compare runs later, like a finding id.
- **Cache**: runs are held in memory, at most 2 (one bank is about 400 MB at
  peak). An evicted run is rebuilt on the next request to the same id only if it
  was created from a seed; for a data folder the caller posts again.
- `POST /runs` with the same input returns the existing id with 200; a new run
  returns 201. Generating seed 7 and running it takes about 3.2 s locally, so
  the call is synchronous; no job queue.

## 2. Safety

The API reads data from disk, so the boundary matters even for a demo.

- **No free paths.** `data_dir` is a folder *name* under `AWB_DATA_ROOT`
  (default: no root, so only `seed` works). The resolved path must stay inside
  the root (`Path.resolve()` plus `is_relative_to`), otherwise 400. Names with
  `/`, `..` or an absolute path are rejected before resolving.
- **Seeds** are validated like the dashboard: whole number 0 to 2,147,483,647.
- **Read-only**: no upload, no delete, nothing written except temporary
  directories for generated banks, removed when the run leaves the cache.
- **Bind address** defaults to `127.0.0.1`. The container sets `0.0.0.0`
  explicitly, behind the compose port mapping.
- No authentication: the API serves synthetic data and is meant for local or
  container use. Section 9 states this as a boundary, not as a gap to close
  later without a spec.

## 3. Command

```
awb api [--host 127.0.0.1] [--port 8000] [--data-root DIR]
```

Starts uvicorn with the app. `--data-root` sets `AWB_DATA_ROOT`.

## 4. Code layout

- `src/audit_workbench/api/app.py`: FastAPI app, routes only.
- `src/audit_workbench/api/runs.py`: run id, cache, path resolution. No FastAPI
  import, unit-tested on its own.
- `src/audit_workbench/api/schemas.py`: pydantic response models. `Finding` is
  already pydantic and is returned as is.
- The detail and overview responses reuse `dashboard/views.py` (`overview`,
  `finding_detail`, `evidence_quality`) so the API and the dashboard cannot
  disagree. Views stay free of Streamlit and FastAPI imports (the guard test is
  extended).

## 5. Docker

- One image for both front ends, `Dockerfile` at the repository root.
  - Multi-stage: build stage with uv (`uv sync --locked --all-extras --no-dev`),
    runtime stage `python:3.12-slim` with only the virtual environment and the
    repository files the app reads (`src/`, `regulation/`, `dashboard/`).
  - Non-root user, `PYTHONDONTWRITEBYTECODE=1`, no compiler in the final image.
  - Default command `awb api --host 0.0.0.0 --port 8000`.
  - `HEALTHCHECK` on `/health`.
- `docker-compose.yml` with two services from the same image: `api` (port 8000)
  and `dashboard` (`awb dashboard --no-browser --host 0.0.0.0 --port 8501`,
  healthcheck on `/_stcore/health`). An optional `./data` volume, mounted
  read-only at `/data` and set as `AWB_DATA_ROOT`.
- `.dockerignore` keeps `.venv`, `.git`, `data/generated`, caches and test
  output out of the build context.
- Dependabot gets the `docker` ecosystem for the base image.

**Local limit:** there is no Docker on this Mac. The image is built and tested
only in CI (section 7). Nothing in the README claims a local Docker check.

## 6. Dependencies

Optional extra `api`: `fastapi` and `uvicorn`. `uvicorn` and `starlette`
already come with Streamlit 1.66, so the lock file grows by little. `httpx` for
the FastAPI test client goes into the dev group.

## 7. Tests

- **Contract**: for seed 7, `GET /runs/{id}/findings` (all pages) returns the
  same 131 finding ids, in the same order, as `awb run --format json`.
- **Summary**: KPIs equal the dashboard overview (131, 7 CRITICAL, 27 of 27,
  123 entities); evidence quality 131 of 131, 0 unexpected.
- **Detail**: the A-05 finding 98d47d38be3341e4 returns its two access paths
  and both regulation quotes; an unknown finding id is 404.
- **Run id**: the same seed and options give the same id; another `as_of` gives
  another id; a second `POST` returns 200 with the existing id.
- **Safety**: `data_dir` values `../x`, `/etc`, `a/b` and a symlink pointing out
  of the root are rejected with 400; a valid folder under a temporary root
  works; invalid seeds are 422.
- **Workpaper**: the DOCX from the API has the same extracted text as
  `awb workpaper` with the same `SOURCE_DATE_EPOCH`; without the extra the
  endpoint answers 501.
- **Paging**: `limit` above 500 is 422; `offset` beyond the end gives an empty
  list with the total count in the response.
- **CI job `docker`**: builds the image, starts `docker compose up -d`, waits
  for both healthchecks, then calls `POST /runs` with seed 7 and checks 131
  findings, and fetches the dashboard root with status 200. Not a required
  check until it has run green on `main` once.

## 8. Build steps (one PR, branch `feat/api-docker`)

1. `api/runs.py` with run id, cache and path resolution, tests first.
2. `api/app.py` and `schemas.py`, contract and safety tests with the test
   client.
3. `awb api` command and the extra `api`; README section with a real
   request and response for seed 7.
4. `Dockerfile`, `.dockerignore`, `docker-compose.yml`, Dependabot entry.
5. CI job `docker`; the PR is ready only when that job is green.

## 9. Deliberately not in this spec

- Authentication, users, rate limits: synthetic data, local or container use.
- Writing endpoints (uploads, rule configuration): the API mirrors the CLI.
- Publishing the image to a registry: possible later with its own decision.
- An API on Streamlit Community Cloud: the cloud only runs the dashboard.

## 10. Open decisions (for approval)

- **F1 Data access.** Seeds always, data folders only under an explicit
  `AWB_DATA_ROOT` (recommended), or seeds only.
- **F2 Dependencies.** Optional extra `api` (recommended, like `workpaper` and
  `dashboard`), or normal dependencies.
- **F3 Base image.** `python:3.12-slim`, the lowest supported version and the
  one CI tests (recommended), or 3.14 like the cloud.
- **F4 Compose.** Both services, API and dashboard, from one image
  (recommended), or the API only.
- **F5 Registry.** No published image for now (recommended), or push to GitHub
  Container Registry on every merge.
