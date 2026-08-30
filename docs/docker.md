# Running in Docker

The project ships as a single container image so the reconciliation + agent
pipeline runs anywhere a container runs — cron, Airflow, Argo, a Kubernetes
Job, or a plain `docker run` — not only inside GitHub Actions. See
[`architecture.md`](architecture.md#10-distribution-the-docker-model) for *why*
it's packaged this way rather than as a GitHub App.

The image contains only application code and pinned dependencies. It contains
**no** credentials, no `.env`, and no seeded or warehouse databases — all of
that is supplied at runtime.

---

## What runs inside the container

One generic entrypoint, `scripts/run_check.py`, with two subcommands that map
1:1 to the project's two triggers:

| Command | What it does | LLM? |
|---|---|---|
| `data-load` | Deterministic reconciliation (row counts, sums, sampled rows) vs. per-environment thresholds. Appends a `trigger_type="data_load"` run to the results store. | No |
| `code-change` | Reconciliation **+** `classify_discrepancy` (the one LLM call). Appends a `trigger_type="code_change"` run with the classification columns filled in. | Yes, when a check is flagged |

By default `code-change` stops after classification. Pass `--run-actions` to
also run the Confluence / Jira / Slack branch nodes (those need their own env
vars — see below).

Both commands, unless given `--no-build`, first run the same EL + `dbt run`
steps the CI workflows do inline: copy the source tables into the DuckDB
warehouse, then build the landing/prep/serve models. Pass `--no-build` when
your orchestrator has already produced `DUCKDB_PATH`.

```
run_check.py data-load    [--environment dev] [--seed] [--no-build] [--fail-on-flag]

run_check.py code-change  --sql-diff-file F  --pr-description-file F
                          [--environment dev] [--seed] [--no-build]
                          [--run-actions] [--fail-on-flag]
```

`--seed` populates the Postgres source with synthetic data first — for demos
and throwaway environments only, never a real source system.

---

## Configuration

### Environment variables

Same names as [`.env.example`](../.env.example), so anyone who has run the
project locally already knows them.

| Variable | Needed for | Notes |
|---|---|---|
| `POSTGRES_CONNECTION_STRING` | always | the source ERP |
| `DUCKDB_PATH` | always | dbt warehouse file; defaults to `/data/warehouse.duckdb` in the image |
| `RESULTS_STORE_PATH` | always | append-only run history; defaults to `/data/results.duckdb` in the image |
| `ANTHROPIC_API_KEY` | `code-change` (when flagged) | the classification LLM call |
| `TARGET_ENVIRONMENT` | optional | default for `--environment` (default `dev`) |
| `SEED_NUM_ORDERS` | optional | row volume for `--seed` (default `3000`) |
| `CONFLUENCE_*`, `JIRA_*`, `SLACK_WEBHOOK_URL` | `code-change --run-actions` only | see `.env.example` for the full list |

Never bake these into an image or commit them. Pass them with `--env-file`,
`-e`, your orchestrator's secret mechanism, or (locally) compose reading your
shell environment.

### Volumes

| Mount | Why |
|---|---|
| `-v $(pwd)/config/environments.yml:/app/config/environments.yml:ro` | supply your own thresholds and auto-action permissions without rebuilding |
| `-v <somewhere>:/data` | persist the warehouse and the results store across runs |

The dashboard is a separate concern — it reads `RESULTS_STORE_PATH` and is
published from CI (`publish_dashboard.yml`). If you want it from the container,
run `python scripts/generate_dashboard.py <out.html>` against the same
`/data` volume.

---

## Build the image

```bash
docker build -t data-observability-agent .
```

The build needs no network access to anything but the package registries. It
installs `uv==0.11.28`, then `uv sync --frozen --no-dev` against the committed
`uv.lock` — the exact dependency set the project is tested with.

> If `pip install uv==0.11.28` ever fails to resolve (yanked release, etc.),
> bump the pin in the `Dockerfile` to any current `uv` — the flags used are
> stable across 0.5+.

---

## Run it — against your own infrastructure

```bash
# nightly data-load check
docker run --rm \
  -e POSTGRES_CONNECTION_STRING="postgresql://user:pass@db.internal:5432/erp" \
  -e DUCKDB_PATH=/data/warehouse.duckdb \
  -e RESULTS_STORE_PATH=/data/results.duckdb \
  -v /var/lib/data-observability:/data \
  -v "$(pwd)/config/environments.yml:/app/config/environments.yml:ro" \
  data-observability-agent data-load --environment prd --fail-on-flag
```

```bash
# code-change check on a dbt PR
docker run --rm \
  -e POSTGRES_CONNECTION_STRING="postgresql://user:pass@db.internal:5432/erp" \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -v /var/lib/data-observability:/data \
  -v "$(pwd)/config/environments.yml:/app/config/environments.yml:ro" \
  -v "$(pwd)/pr:/pr:ro" \
  data-observability-agent code-change \
    --environment qa \
    --sql-diff-file /pr/models.diff \
    --pr-description-file /pr/description.txt
```

Produce `models.diff` however your CI does — e.g.
`git diff <base> <head> -- dbt_project/models > pr/models.diff`.

---

## Run it — local demo with docker-compose

`docker-compose.yml` spins up the container **plus a throwaway Postgres**, wired
together on the compose network. No real database, no host ports, no accounts.

```bash
# seeds Postgres (3000 orders), builds the dbt warehouse, runs the data-load check
docker compose up --build
```

Expected tail:

```
Reconciliation: 6 results, 6 flagged.
  [FLAG] aggregate vbap -> prep_sales_orders      row_count            diff_pct=17.26 threshold=5.00
  [FLAG] aggregate vbap -> prep_sales_orders      sum_net_value        diff_pct=17.44 threshold=5.00
  ...
Wrote run_id=... to results_store (6 flagged).
```

(The ~17% divergence is the intended cancelled-order exclusion in
`prep_sales_orders` — see `architecture.md`.)

Then run the LLM path against the bundled example PR (needs `ANTHROPIC_API_KEY`
in your shell or a local `.env`):

```bash
docker compose run --rm agent code-change \
  --sql-diff-file examples/sample_sql_diff.txt \
  --pr-description-file examples/sample_pr_description.txt
```

Expected tail:

```
classify_discrepancy: final=expected confidence=0.80 downgraded=False (llm raw=expected)
Wrote run_id=... to results_store.
```

Inspect what was written:

```bash
docker compose run --rm --entrypoint python agent -c \
  "import duckdb,os; c=duckdb.connect(os.environ['RESULTS_STORE_PATH'],read_only=True); \
   [print(r) for r in c.execute('select trigger_type,metric,round(diff_pct,2),status,final_classification,confidence from results order by run_timestamp').fetchall()]"
```

Tear down (removes the Postgres data and the `/data` volume):

```bash
docker compose down -v
```

---

## How this relates to the CI path

The GitHub Actions workflows (`on_data_load.yml`, `on_dbt_change.yml`) still use
their own thin entrypoints — `scripts/run_data_load_check.py` and
`scripts/run_code_change_check.py` — because they do GitHub-specific work
(reading `PR_BASE_SHA`/`PR_HEAD_SHA`, posting the PR summary comment, pushing the
updated results store). `scripts/run_check.py` shares the **same** reconciliation
and agent code underneath; it just takes every input from CLI args and env vars
instead of assuming the Actions runner. Behavior of the checks themselves is
identical either way.
