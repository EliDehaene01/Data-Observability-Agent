# Data-Observability-Agent

An agentic reconciliation system that verifies dbt views stay accurate and
complete against source ERP systems (e.g. SAP). It runs **deterministic checks**
for scheduled data-load validation, and uses **LLM-based reasoning** to classify
code-change discrepancies as `expected`, `needs_review`, or `anomaly` — with
automated Confluence / Jira / Slack follow-up. On a dbt PR it also blocks the
merge when the classification isn't a clean `expected`, and refreshes per-model
Confluence documentation for every changed model.

> Portfolio project. Built vendor-agnostic and cheap: local DuckDB / Postgres,
> GitHub Actions free tier, free-tier Confluence / Jira / Slack. Real SAP and
> cloud infra are designed-for swap-ins, not built here.

---

## The problem

When a dbt model's output diverges from its source system, a raw reconciliation
job flags every case the same way — *target is 17% below source*. But there are
two very different causes:

- **Intended:** a PR added a business rule (e.g. *exclude cancelled orders*) and
  the numbers moved as a direct consequence.
- **Broken:** a load half-failed, an upstream feed changed shape, or a refactor
  had a side effect nobody meant.

Telling them apart means reading the SQL diff and the PR's stated intent — a
reasoning task. This system separates the deterministic measurement from that
reasoning and only spends an LLM call on the second half.

## How it works — two triggers, two jobs

| | **Data-load validation** | **Code-change validation** |
|---|---|---|
| Trigger | Scheduled (nightly) | dbt PR / deploy |
| Premise | Business logic unchanged → any divergence is a data problem | Business logic changed → is *this* divergence explained by *this* diff? |
| Method | Row counts, `sum(net_value)`, sampled row comparison vs. per-environment thresholds | Reconciliation **+** a LangGraph agent that classifies the discrepancy |
| LLM? | **Never** | **Only here.** One node for *validation* (`classify_discrepancy`); a second, separate node writes model docs (`summarize_model_logic`). Both forced structured output. |
| Entry point | `.github/workflows/on_data_load.yml` | `.github/workflows/on_dbt_change.yml` |

The agent's classification then branches:

```
expected      → draft Confluence update → publish docs
needs_review  → draft summary           → post Slack notification
anomaly       → create Jira ticket      → notify team
```

A **deterministic safety layer** sits between the LLM and that branch: an
under-confident `expected` is downgraded to `needs_review`, and a PR that claims
"no impact" while its diff says otherwise is forced to `needs_review` regardless
of the model's confidence. See [`docs/architecture.md`](docs/architecture.md) for
the full design, including the real diagnostic that made the second rule
necessary.

## What happens on a dbt PR

When a PR touches `dbt_project/models/**`, `on_dbt_change.yml`:

1. **Reconciles + classifies.** Builds the warehouse at the PR's HEAD, runs the
   deterministic reconciliation, and — if anything is flagged — runs the
   LangGraph agent to classify the discrepancy. The result is posted as a PR
   comment and written to `results_store/`.
2. **Blocks the merge unless the result is `expected`.** The `dbt-change-check`
   job fails when `final_classification` is `needs_review` or `anomaly`, so a PR
   that needs a human can't be merged on green. The audit row is written
   *before* the job fails — a blocked or abandoned PR still leaves history.
3. **Refreshes per-model Confluence docs.** For every changed model in
   `landing/` `prep/` `serve/` it publishes/updates a child page under a
   **"Model Documentation"** parent in Confluence, containing the model's source
   tables, `ref()` lineage and columns/types (deterministic, straight from
   dbt's `manifest.json` + `catalog.json`) plus a plain-English business-logic
   summary (a second, separate LLM call — never used to infer structure). It
   also regenerates a single **"Serve Layer Overview"** data-dictionary page.
   This step never blocks the PR.
4. **Publishes reconciliation reports as nested pages.** An `expected`
   classification publishes its Confluence write-up as a child page under a
   **"Reconciliation Updates"** parent — not a flat top-level page.

### Branch protection (repo owner, one-time manual step)

The blocking behavior above only actually *prevents* a merge once the check is
marked **required**. Claude Code can't change repo settings, so the repo owner
must do this by hand:

> **Settings → Branches → Branch protection rules → `main` → Require status
> checks to pass before merging →** add **`dbt-change-check`**.

Until that's set, the job still runs and still goes red on a blocking
classification — it just won't stop a merge.

### Manual trigger (no PR needed)

`on_dbt_change.yml` also has a `workflow_dispatch` trigger for testing the whole
path without opening a PR:

> **Actions → "PR-triggered code-change validation" → Run workflow →** paste a
> synthetic `sql_diff` (unified-diff text), a `pr_description`, and pick an
> `environment`.

In this mode the scripts use the inputs directly, the real PR comment is skipped
(logged instead), and the results-store commit to `main` is skipped so the
manual run doesn't touch the audit trail or dashboard.

## Architecture at a glance

```
mock_erp/          synthetic SAP-style source data (VBAK/VBAP)
dbt_project/        the codebase under observation — landing / prep / serve layers
connectors/         base.py interface first, swappable drivers behind it
  source/           Postgres (the ERP)
  target/           DuckDB (the dbt warehouse)
  docs/             Confluence
  ticketing/        Jira, Slack
  reporting/        static HTML dashboard
reconciliation/     deterministic engine — plain Python + SQL, no LLM, pydantic output
results_store/      append-only run history (DuckDB); the dashboard's only source
agent/              LangGraph reasoning layer — consumes reconciliation output only
config/             per-environment thresholds & permissions as YAML data
```

Design rules that hold everywhere: `reconciliation/` never calls an LLM;
`agent/` never re-computes a reconciliation result; every external system is
reached through a `base.py` interface; dev/qa/prd differences live in
`config/environments.yml` as data, never as branching code.

## Tech stack

Python 3.13 · [uv](https://docs.astral.sh/uv/) · **dbt** (dbt-core + dbt-duckdb)
for the monitored transformations · **DuckDB** as the warehouse and the
append-only results store · **Postgres** as the source ERP · **LangGraph** for
the agent flow · **Anthropic** (structured tool-use output) for the
classification call and the separate model-doc summary call · **dbt**
`manifest.json` / `catalog.json` as the deterministic source for model lineage
and columns · **pydantic** for every structured payload · GitHub Actions
for the MVP triggers · **Docker** / docker-compose for portable distribution.

## Quickstart (Docker)

The easiest way to see the whole pipeline run — no Postgres, no accounts, no
local Python setup. Needs only Docker.

```bash
docker compose up --build
```

This seeds a throwaway Postgres, builds the dbt warehouse, and runs the
deterministic **data-load** check, printing the reconciliation results (you'll
see the intended ~17% cancelled-order divergence flagged).

To exercise the **code-change** path (the LLM classification), set
`ANTHROPIC_API_KEY` in your shell or a local `.env`, then:

```bash
docker compose run --rm agent code-change \
  --sql-diff-file examples/sample_sql_diff.txt \
  --pr-description-file examples/sample_pr_description.txt
# → classify_discrepancy: final=expected confidence=0.80 downgraded=False
```

Prefer a pre-built image? It's published to GitHub Container Registry on every
push to `main`:

```bash
docker pull ghcr.io/elidehaene01/data-observability-agent:latest
```

Full container usage — building the image, pulling the published one, running it
against your own infrastructure, every env var and volume — is in
[`docs/docker.md`](docs/docker.md).

## Running locally (native)

**Prerequisites:** Python 3.13, [uv](https://docs.astral.sh/uv/), and a local
Postgres with a `mock_erp` database.

```bash
uv sync

cp .env.example .env
# then set POSTGRES_CONNECTION_STRING (and ANTHROPIC_API_KEY for the agent path)
```

Build the source and the dbt target — the same pipeline CI uses:

```bash
uv run python mock_erp/seed_data.py --engine postgres --num-orders 3000
uv run python scripts/load_source_into_duckdb.py
uv run dbt run --project-dir dbt_project --profiles-dir dbt_project
```

Run the checks via the generic entrypoint (same one the container uses):

```bash
# deterministic data-load reconciliation → appends to the results store
uv run python scripts/run_check.py data-load

# reconciliation + LLM classification on a dbt diff (needs ANTHROPIC_API_KEY)
uv run python scripts/run_check.py code-change \
  --sql-diff-file examples/sample_sql_diff.txt \
  --pr-description-file examples/sample_pr_description.txt

# regenerate the static dashboard from the results store
uv run python scripts/generate_dashboard.py public/index.html
```

`scripts/run_data_load_check.py` and `scripts/run_code_change_check.py` are the
GitHub Actions entry points — they additionally post PR comments and push the
results store — and are invoked by the workflows, not run by hand.

## Tests

```bash
uv run pytest              # fast: real local Postgres/DuckDB, Anthropic API mocked
uv run pytest --run-live   # also runs the handful of real-model sanity checks
```

`test_reconciliation.py` and `test_connectors.py` are real integration tests
against local Postgres + DuckDB (no mocking). `test_agent_graph.py` covers the
decision logic with the LLM call mocked. See [`tests/README.md`](tests/README.md).

## Dashboard

`connectors/reporting/html_dashboard.py` generates a self-contained HTML page
from `results_store/results.duckdb` only. `publish_dashboard.yml` regenerates and
deploys it to GitHub Pages whenever either trigger commits new results. Swapping
in Power BI / Metabase later is a new `ReportingConnector` class and nothing
else.

For ad-hoc slicing there's also a **local Power BI companion** — a committed
PBIP / TMDL project in [`powerbi/`](powerbi/) over CSVs exported from the same
results store (`scripts/export_results_for_powerbi.py`). It's a manual, local
artifact (Power BI Desktop can't run headless), not part of CI — see
[`docs/powerbi.md`](docs/powerbi.md).

## License

[MIT](LICENSE)
