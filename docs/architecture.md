# Architecture

`data-observability-agent` verifies that dbt views (transformation outputs) stay
accurate and complete relative to the source system they're built from — a
SAP-style ERP in this project, driven by synthetic VBAK/VBAP-style data in
`mock_erp/`.

The interesting problem isn't diffing numbers. It's telling two situations apart:

- **"The numbers changed because the SQL intentionally changed."** A dbt PR added
  a business rule — say, *exclude cancelled orders* — and downstream row counts
  dropped as a direct, expected consequence.
- **"The numbers changed because something broke."** A load job half-failed, an
  upstream feed changed shape, or a refactor had a side effect nobody intended.

A raw reconciliation job flags both identically: *target diverges from source by
17%*. Deciding which one you're looking at requires reading the SQL diff and the
PR's stated intent — a reasoning task. This system splits the deterministic
measurement from that reasoning, and only spends an LLM call on the second half.

It's a portfolio project: built vendor-agnostic and cheap (local DuckDB/Postgres,
GitHub Actions free tier, free-tier Confluence/Jira/Slack). Real SAP and real
cloud infra are designed-for swap-ins, not built here.

---

## 1. Two triggers, two jobs

The core design decision is that there are **two separate jobs**, and they never
share a code path.

### Data-load validation — scheduled, deterministic

Runs on a schedule (e.g. nightly). Business logic hasn't changed since the last
run, so any divergence beyond tolerance is a data-integrity problem by
definition. The check is pure arithmetic: row counts, `sum(net_value)`, and
sampled row-level comparisons, each diffed against a per-environment threshold.

**No LLM call belongs in this path.** There's nothing to reason about — either the
numbers reconcile or they don't.

Entry point: `.github/workflows/on_data_load.yml` → `scripts/run_data_load_check.py`.

### Code-change validation — PR-triggered, reasoning

Runs when a dbt model changes (PR / deploy). Here the business logic *did* change,
so a divergence is expected up to a point — the question is whether *this*
divergence is explained by *this* diff. The agent reads the SQL diff, the
reconciliation results, and the PR description, and classifies the discrepancy as
`expected`, `needs_review`, or `anomaly`.

**This is the only path that invokes an LLM.**

Entry point: `.github/workflows/on_dbt_change.yml` → `scripts/run_code_change_check.py`
→ the LangGraph agent.

### Why keep them apart

Conflating them is the classic way this kind of tool rots. If the scheduled job
starts calling an LLM "just to be safe," you've added cost, latency, and
non-determinism to a check that had a provably correct answer. If the PR job
skips the reasoning step, every intentional business-logic change pages a human.
The split keeps each job honest about what it actually knows.

---

## 2. The deterministic / LLM boundary

The split above is enforced structurally, not by convention:

| Layer | Allowed to reason? | Contract |
|---|---|---|
| `reconciliation/` | **No.** Plain Python + SQL. | Emits pydantic models (`ReconciliationResult`, `ReconciliationRun`) and nothing else. |
| `results_store/` | **No.** Append-only inserts. | One row per `ReconciliationResult`. Never updates or deletes. |
| `agent/` | **Yes**, in exactly two nodes (see §5a). | Consumes `reconciliation/`'s structured output (validation) or a model's compiled SQL (docs). Never re-computes a reconciliation result, never infers model structure. |
| `model_docs/` | **No.** Parses dbt artifacts + a diff. | Reads `manifest.json` / `catalog.json`; emits pydantic (`ModelStructure`, `ServeField`) and rendered page bodies. |
| `connectors/` | **No.** | `base.py` interface first; concrete drivers behind it. |

`reconciliation/aggregate_checks.py` and `sample_checks.py` compare numbers and
apply a threshold. That's the whole job. They don't special-case the known
cancelled-order divergence, even though they "know" it's coming — they report
`diff_pct` like any other check and let the threshold (and later the agent)
decide what it means. If a piece of logic in here wants to *interpret* a result,
that's the signal it belongs in `agent/` instead.

`agent/` only ever reads `ReconciliationResult` objects. It never re-queries a
database to double-check a number. The one LLM node (`classify_discrepancy`)
takes the already-computed results as evidence and reasons *over* them.

### Structured LLM output, always

`classify_discrepancy` uses Anthropic tool-use with `tool_choice` forced to a
single tool whose `input_schema` pins the output shape:

```
classification:       "expected" | "needs_review" | "anomaly"
confidence:            0.0 – 1.0
reasoning:             2–4 sentences (human-facing; goes into Confluence/Slack)
pr_claims_no_impact:   boolean
```

The model never returns free text that downstream code has to parse. The
branch the graph takes is a dict key lookup, not a regex over prose.

### Two LLM uses, two nodes

There are exactly **two** places an LLM is called, and they are separate nodes
with separate jobs:

| | `classify_discrepancy` | `summarize_model_logic` |
|---|---|---|
| Purpose | **Validation** — is this discrepancy `expected` / `needs_review` / `anomaly`? | **Documentation** — describe, in plain English, what business logic a changed model performs. |
| Input | reconciliation results + SQL diff + PR description | one model's compiled SQL |
| In `agent/graph.py`? | Yes — it's the graph's reasoning step | **No** — called directly by `scripts/generate_model_docs.py` |
| Gates anything? | Yes — its result blocks or clears the PR | No — pure documentation |
| Structural facts | reasons *over* them, doesn't produce them | **never** produces them — see below |

`summarize_model_logic` (`agent/nodes/summarize_model_logic.py`) only ever
describes *logic*. A model's source tables, `ref()`/`source()` lineage, and
column names/types are read deterministically from dbt's own `manifest.json`
(lineage) and `catalog.json` (warehouse-observed columns + types) in
`model_docs/manifest.py`. Those artifacts are authoritative and
hallucination-free; asking an LLM to infer them from raw SQL would be strictly
worse. So the per-model doc page is half deterministic (structure) and half
generated (the logic paragraph), and the page itself labels which half is
which.

---

## 3. The connector abstraction

Every external system is reached through a `base.py` interface. Concrete
implementations are swappable without touching `reconciliation/` or `agent/`.

```
connectors/
  source/     base.py  → postgres_source.py        (the ERP)
  target/     base.py  → duckdb_target.py           (the dbt warehouse)
  docs/       base.py  → confluence.py
  ticketing/  base.py  → jira.py, slack.py
  reporting/  base.py  → html_dashboard.py
```

`SourceConnector` and `TargetConnector` expose the same four methods —
`get_row_count`, `get_aggregate`, `sample_rows`, `get_schema` — and every one of
them takes plain table/column names and returns plain Python values (`int`,
`float`, `list[dict]`, `list[ColumnInfo]`). Never a driver cursor, never an
engine-specific row type. That consistency is the entire point: `reconciliation/`
runs the identical code against a Postgres source and a DuckDB target because
both look the same from where it sits.

Adding a new source system (a real SAP connection, Snowflake, BigQuery) means
writing one new class against the existing interface — not adding a branch to
calling code.

### Confluence page hierarchy

`DocsConnector.publish_page` takes an optional `parent_title` and a
`content_format` (`"text"` | `"storage"`). The Confluence implementation never
creates flat top-level pages: it ensures the parent/section page exists —
looking it up first, creating it once only if missing, reusing it forever
after — and publishes the real page as a **child** via the REST API's
`ancestors` parameter.

```
Space  (CONFLUENCE_SPACE_KEY, e.g. SD)
├── Reconciliation Updates          ← parent, auto-created once
│   ├── Reconciliation update - dev - 2026-09-01 16:39:05 UTC - 22c41a9e
│   └── …                            (one child per classified run, unique titles)
└── Model Documentation             ← parent, auto-created once
    ├── dbt model: landing_vbak
    ├── dbt model: prep_sales_orders (stable title → updated in place, version bumped)
    ├── …
    └── Serve Layer Overview         (single data-dictionary page, regenerated)
```

Publishing is an **upsert**: a page whose title already exists in the space is
updated (version incremented), not duplicated. Reconciliation reports use
unique per-run titles (so effectively create-only); per-model doc pages use
stable titles (so a re-run of the same PR revises the same page). A non-tree
`DocsConnector` implementation is free to ignore `parent_title`.

### Injection-safe by construction

`SourceConnector` / `TargetConnector` never interpolate an aggregate function or
a filter operator into SQL as a raw string. Both are checked against frozen
whitelists in `base.py` (`SUPPORTED_AGG_FUNCS = {sum, avg, count, min, max}`,
`COMPARISON_OPERATORS = {=, !=, >, >=, <, <=}`); anything else raises `ValueError`
before a query is built. Filter *values* go through the driver's parameter
binding. Identifiers are quoted. `tests/test_connectors.py` fires
`"; drop table vbap; --"` at both engines and asserts the rejection.

---

## 4. The dbt project being monitored

`dbt_project/` is the codebase under observation, not part of the monitoring
system. Its models are in three layers — deliberately *not* the generic
staging/marts split — because the layer a discrepancy shows up in tells you what
it means:

- **`models/landing/`** — `landing_vbak`, `landing_vbap`. 1:1 with source.
  Cleaned and typed only: no filtering, no business logic. **Any** divergence
  from source here is a bug, full stop — there's no design decision that could
  explain it. (This is also why `reconciliation/` queries the raw `vbap` source
  table directly rather than `landing_vbap`: they're defined to be identical, so
  hitting the real source avoids routing a "source-side" number through the
  warehouse.)

- **`models/prep/`** — `prep_sales_orders`. **Where business logic lives.**
  Intentional source-to-target divergence is introduced here and commented:
  cancelled orders are excluded entirely, incomplete orders are flagged. This is
  exactly the layer `classify_discrepancy` has to reason about — an `expected`
  classification almost always traces to a rule added here.

- **`models/serve/`** — `serve_sales_orders`, `serve_monthly_revenue`.
  Business-friendly renames and aggregates only. **No new logic ever.** If
  `serve_sales_orders`'s divergence from source isn't identical to
  `prep_sales_orders`'s, something leaked a rule into the serve layer — and
  `tests/test_reconciliation.py` asserts exactly that equality.

dbt's own tests (`dbt_project/tests/`) guard the invariants: keys unique,
referential integrity, and no cancelled orders leaking through to prep or serve.

---

## 5. The safety mechanism: confidence gating + PR-honesty override

The LLM produces a *raw* `(classification, confidence, reasoning,
pr_claims_no_impact)` tuple. It does **not** decide the classification the graph
branches on. `_apply_decision_logic` in `agent/nodes/classify_discrepancy.py`
does, with two deterministic rules applied in order.

### Rule 1 — confidence gating

An `expected` classification is only trusted at or above the current
environment's `confidence_threshold` (`config/environments.yml`). Below it,
downgrade to `needs_review` — an under-confident "this is fine" is not good
enough to auto-publish a docs update and move on.

`needs_review` and `anomaly` are **never** gated on confidence. A low-confidence
"something's wrong" still means a human should look; gating it would be
backwards.

Thresholds climb by environment — `dev` 0.6, `qa` 0.75, `prd` 0.9 — so the same
code path is progressively stricter about auto-acting as you approach
production. prd also has `can_publish_docs: false` and
`requires_human_approval: true`: it escalates rather than auto-resolves.

### Rule 2 — PR-honesty override

Independent of confidence entirely. If **all** of:

- `pr_claims_no_impact` is true (the PR says "no behavior change" / "safe"), **and**
- `diff_touched_tables` is non-empty (the diff actually touches a table involved
  in the flagged discrepancy), **and**
- a reconciliation result actually exceeded its threshold,

then force `needs_review` — regardless of the raw classification or confidence.
This can override a raw `anomaly` down to `needs_review`, the same way `anomaly`
is otherwise never gated.

### The real gap that made Rule 2 necessary

Rule 2 isn't speculative. It came out of a live diagnostic: 10 repeated runs of
the same scenario — *PR description claims no impact, but the SQL diff plainly
disagrees and a metric is off*.

In **8 of 10 runs** the model scored `confidence` at **0.72–0.75** — comfortably
above the dev threshold of 0.6 — for an `expected` classification, **even while
its own `reasoning` text said the PR's claim was "contradicted" / "factually
wrong."**

The lesson: `confidence` and PR-honesty are two different judgments.
`confidence` measures *does the diff explain the metric's direction and
magnitude* — and mechanically, a diff that removes cancelled orders **does**
explain a row-count drop, so high confidence is arguably correct. It does **not**
measure *is the PR's own narrative about impact honest*. Rule 1, which only looks
at `confidence`, sails straight past a dishonest PR. So the model now scores the
two things as two separate fields, and two independent deterministic checks
consume them.

### The edge case that `diff_touched_tables` guards

The first version of Rule 2 didn't have the `diff_touched_tables` condition. It
broke a different case, caught by a regression check:

A genuinely no-op change — a one-line comment added to `landing_vbak.sql` — with
a real, unrelated divergence in `serve_monthly_revenue`. The comment PR honestly
"claims no impact," so `pr_claims_no_impact=True` is *correct*. But that's
irrelevant information, not PR dishonesty about the flagged discrepancy — the
diff never went near the diverging table. Without the guard, Rule 2 fired and
downgraded a correct `anomaly` (nothing explains this divergence) to
`needs_review`.

The `diff_touched_tables` non-empty condition fixes it: a "no impact" claim is
only *dishonest about this discrepancy* if the diff actually touches the affected
table. When `diff_touched_tables` is empty, `anomaly` stands — which is the right
answer for a divergence in a table nobody changed.

`tests/test_agent_graph.py` pins all of this: both override-fires cases, the
empty-`diff_touched_tables` case, the honest-PR case, and the
no-actual-flag case.

---

## 5a. The required status check (PR blocking)

`on_dbt_change.yml`'s `dbt-change-check` job is designed to be a **required
status check** on `main`. It fails — turning the PR's check red and, once
branch protection requires it, blocking the merge — whenever the agent's
`final_classification` is `needs_review` or `anomaly`. `expected` is the only
non-blocking outcome.

### How the exit code is produced

`scripts/run_code_change_check.py` doesn't exit non-zero itself. It runs the
full pipeline, writes the `results_store/` audit row, then writes
`blocking=true|false` and `final_classification=…` to `$GITHUB_OUTPUT`. The
workflow's **last** step reads that output and does `exit 1` when
`blocking == true`.

The ordering matters and is deliberate:

```
classify → (Confluence model docs) → commit results to main → dashboard → ENFORCE
```

The audit trail is committed *before* the enforcing step fails the job. A
blocked PR, or one that's later abandoned, still leaves a full history in
`results_store/` and on the dashboard — the same principle as "both workflows
commit straight to `main`". Failing the check must never cost the audit row.

### Why a job exit code, not a Commit Status API call

A separate `POST /statuses` call would need `statuses: write` and its own
bookkeeping for the SHA and context string. The job's own conclusion is
already a check GitHub knows about by name (`dbt-change-check`); making *that*
red is the simplest thing that becomes a real required check with zero extra
permissions. The repo owner enables the requirement once under **Settings →
Branches** (documented in the README — code can't change repo settings).

### `workflow_dispatch` mode

The workflow also has a manual `workflow_dispatch` trigger with `sql_diff`,
`pr_description`, and `environment` inputs, so the whole path can run without
opening a PR. In that mode `GITHUB_EVENT_NAME=workflow_dispatch` and the
scripts:

- read `SYNTHETIC_SQL_DIFF` / `SYNTHETIC_PR_DESCRIPTION` directly instead of
  `git diff`-ing a PR range,
- log the would-be PR comment instead of posting it (there's no PR),
- **skip** the results-store commit to `main` (a synthetic test must not
  pollute the real audit trail or dashboard).

The blocking-policy step still runs, so a manual run with a
`needs_review`/`anomaly` scenario still goes red — which is how you test the
gate.

---

## 5b. The per-model documentation pipeline

On the same PR trigger (path-filtered to `dbt_project/models/**`),
`scripts/generate_model_docs.py` regenerates Confluence documentation for every
changed model. It is **documentation, not validation**: the workflow step is
`continue-on-error`, so a Confluence outage can't block a PR.

### Deterministic structure, generated logic

```
manifest.json  ──►  source() tables, ref() lineage, schema.yml columns   ┐
catalog.json   ──►  complete column list + warehouse data types          ├─►  ModelStructure  (deterministic)
                                                                         ┘
compiled SQL   ──►  summarize_model_logic (LLM, forced structured)       ──►  ModelLogicSummary (generated)
```

`dbt docs generate` (a workflow step added before this one) produces
`catalog.json` by inspecting the built warehouse — it's the authoritative,
complete column list, which `manifest.json` alone is not (manifest only carries
the columns documented in `schema.yml`). If `catalog.json` is somehow missing,
`model_docs/manifest.py` falls back to the manifest's documented subset and
marks the page as possibly incomplete.

### What gets published

- **One child page per changed model** in `landing/` `prep/` `serve/`, under a
  **"Model Documentation"** parent: source tables, `ref()` lineage, a
  field/type table, and the LLM logic summary + key transformations. Stable
  title (`dbt model: <name>`) → updated in place on re-run.
- **One "Serve Layer Overview" page**, regenerated whenever any model changed:
  a data-dictionary table of every field across all `serve_` models, with its
  business-friendly name (which *is* the serve column name — the serve layer is
  rename-only, §4) and data type. No LLM involved.

The changed-models list is parsed deterministically from the unified diff
(`changed_model_names_from_diff`) — the same code path for a real `git diff`
and a `workflow_dispatch` synthetic diff.

### Why it's not a LangGraph node

`summarize_model_logic` is a plain function, not a node in `agent/graph.py`. It
has no branching, no state to carry, and runs on a different cadence (once per
changed model, outside the validation flow). Wiring it into the graph would
couple documentation generation to the validation state machine for no benefit.

---

## 6. LangGraph flow

`agent/graph.py`:

```
fetch_reconciliation_results → analyze_diff → classify_discrepancy   (LLM, structured output)
    → branch on the (possibly downgraded) final_classification:
        expected      → draft_confluence_update → publish_docs
        needs_review  → draft_summary           → post_slack_notification
        anomaly       → create_jira_ticket      → notify_team
```

Every node except `classify_discrepancy` is deterministic Python. The action
nodes call the real Confluence / Jira / Slack connectors. The second LLM call,
`summarize_model_logic`, is **not** in this graph (see §5b).

`AgentState` (`agent/state.py`) is the single shared schema carried across nodes
— reconciliation results, diff context, classification, drafted outputs. Nodes
don't invent their own ad hoc state shapes.

**`post_slack_notification` is a one-way notification, not an approval step.**
Real human-in-the-loop approval needs a hosted callback endpoint to receive the
human's response, which this MVP doesn't have. That's a deliberate limitation,
documented so nobody "fixes" the name back to something implying interactive
approval before the callback exists.

---

## 7. Environment awareness as data

dev / qa / prd behavior differences live entirely in
`config/environments.yml` — thresholds, `confidence_threshold`, and
`auto_actions` permissions (`can_publish_docs`, `can_create_ticket`,
`can_auto_resolve`, `requires_human_approval`). The code path is **identical**
across environments; it just reads config.

```yaml
dev:  row_count_diff_pct: 5.0   confidence_threshold: 0.6   can_publish_docs: true
qa:   row_count_diff_pct: 2.0   confidence_threshold: 0.75  requires_human_approval: true
prd:  row_count_diff_pct: 0.5   confidence_threshold: 0.9   can_publish_docs: false
```

No `if environment == "prd"` anywhere in Python. Adding a fourth environment is a
YAML edit.

---

## 8. Results store and dashboard

Both trigger workflows write their results to `results_store/results.duckdb`
(a DuckDB file separate from the dbt target `dev.duckdb`, so the audit trail
survives independently of the warehouse). The store is **append-only**: one row
per `ReconciliationResult`, never updated or deleted.

```
run_id, environment, trigger_type, run_timestamp, check_type, table, metric,
source_value, target_value, diff_pct, threshold, status,
final_classification, confidence, pr_claims_no_impact, downgraded
```

The last four columns are only populated for `trigger_type="code_change"` runs —
`data_load` runs leave them `NULL`, because data-load validation never
classifies anything. Those four were added *after* the table already had rows;
`writer.py` uses `ALTER TABLE ADD COLUMN IF NOT EXISTS` so old rows keep reading
fine with `NULL` in the new columns.

**The dashboard only ever reads from the results store** — never from live
reconciliation output or agent state. It goes through the `ReportingConnector`
interface (`generate_report(output_path)`), whose one MVP implementation
(`html_dashboard.py`) produces a static HTML page published to GitHub Pages by
`.github/workflows/publish_dashboard.yml` (triggered on push to `main`,
path-filtered to `results_store/results.duckdb`). Swapping in Power BI or
Metabase later means a new `ReportingConnector` class and nothing else — the
store and the reconciliation engine don't move.

Both trigger workflows commit their results straight to `main` regardless of
which branch triggered them, so a PR that never merges still leaves an audit
trail.

### The Power BI companion (local, manual)

Alongside the automated HTML dashboard there's a **local Power BI Project**
(`powerbi/`, PBIP + TMDL — all text, committed). `scripts/export_results_for_powerbi.py`
reads the results store through the same `results_store.reader` path the
dashboard uses and writes two CSVs (`reconciliation_results.csv`,
`classification_history.csv`, gitignored); the TMDL semantic model imports
those and defines the measures (`Flag Rate %`, the classification counts,
`Average Confidence`, `Runs by Environment`, `PR-Honesty Override Fire
Count`).

It is **not** CI-automated, and can't be: Power BI Desktop is a Windows GUI
application with no headless mode, so no GitHub Actions job can open the
`.pbip`, refresh it, or emit a `.pbix`. Refreshing means running the export
script and clicking Refresh in Desktop by hand. This is the same shape of
deliberate MVP limitation as `post_slack_notification` being notify-only
(§6) — the runtime that would make it automatic doesn't exist here, so it
ships as a documented local tool rather than a faked pipeline step.

It **complements, not replaces** the HTML dashboard: the dashboard is the
always-current, zero-setup, auto-published view (GitHub Pages, regenerated on
every results commit); the Power BI companion is for someone who wants to
pivot and chart the same data interactively. Both read only from
`results_store/` — neither touches reconciliation or agent internals. Full
usage in [`powerbi.md`](powerbi.md).

---

## 9. Tests

`tests/` — see `tests/README.md` for how to run.

- **`test_reconciliation.py`** / **`test_connectors.py`** — real integration
  tests against local Postgres + DuckDB (no mocking). They build the actual
  landing/prep/serve pipeline once per session and assert: thresholds are read
  per-environment from `environments.yml`, `status` flips `pass`→`flag` exactly
  at the boundary, the cancelled-order divergence magnitude tracks the actual
  cancelled-order rate in the seed data (computed independently, not hardcoded),
  connector method shapes match across both engines, and the injection whitelist
  rejects garbage.
- **`test_agent_graph.py`** — `classify_discrepancy`'s decision logic with the
  Anthropic call **mocked** by default. Covers every branch of Rules 1 and 2,
  including the `diff_touched_tables` edge case.
- **`test_model_docs.py`** — `model_docs/` deterministic parsing (manifest +
  catalog + diff) against small synthetic artifacts, `render.py` output +
  escaping, and `summarize_model_logic` with the Anthropic call mocked. One
  `@pytest.mark.live` summary check.
- **`test_docs_connector.py`** — the Confluence hierarchy + upsert logic with
  `requests` mocked: parent created once then reused, child published with
  `ancestors`, same-title page updated with an incremented version, storage vs
  text body handling.
- **`@pytest.mark.live`** — a handful of tests that call the real Anthropic API,
  skipped unless `--run-live`. For manual sanity-checking after a prompt change,
  not CI.

Default run — `uv run pytest` — is fast, hits real local databases, and never
touches the Anthropic API.

---

## 10. Distribution: the Docker model

The MVP runs on GitHub Actions, but that's the *demo* substrate, not the
product. The project ships as a **single container image** (`Dockerfile`,
`docs/docker.md`) with one generic entrypoint — `scripts/run_check.py
{data-load,code-change}` — that takes every input from CLI args and environment
variables. It runs unchanged under cron, Airflow, Argo, a Kubernetes `Job`, or a
plain `docker run`.

### Why a container, not a GitHub App

A GitHub App is the obvious-looking packaging for something triggered by dbt
PRs. It was rejected deliberately:

- **Most enterprises don't orchestrate data pipelines through GitHub.** The
  dbt deploy that should trigger a code-change check often runs in Airflow,
  Dagster, Jenkins, GitLab CI, or a vendor's managed scheduler. A GitHub App
  can only react to events on GitHub.
- **The source system is inside the customer's network.** The container runs
  wherever it already has a route to the ERP and the warehouse; a hosted GitHub
  App would need inbound access to private infrastructure.
- **Config and credentials are already environment-shaped.** Every external
  dependency is an env var (`POSTGRES_CONNECTION_STRING`, `ANTHROPIC_API_KEY`,
  `CONFLUENCE_*`, …) and `config/environments.yml` is mounted as a volume — a
  drop-in for anyone who has run the project locally.

A Helm chart on top is a plausible later addition for Kubernetes-native
customers; it doesn't change anything below it.

### What's in the image vs. supplied at runtime

| In the image | Supplied at runtime |
|---|---|
| App code (`agent/`, `reconciliation/`, `connectors/`, `dbt_project/`, …) | `POSTGRES_CONNECTION_STRING`, `ANTHROPIC_API_KEY`, `CONFLUENCE_*` / `JIRA_*` / `SLACK_WEBHOOK_URL` |
| Pinned dependencies (`uv sync --frozen --no-dev`) | `config/environments.yml` (volume mount — customer's own thresholds) |
| A default `config/environments.yml` (so it runs stand-alone) | The DuckDB warehouse + the results store (a `/data` volume) |
| Bundled example PR diff/description for the demo | — |

No `.env`, no credentials, and no seeded/warehouse `.duckdb` files are ever
baked in (`.dockerignore` enforces this).

### The CI entrypoints don't go away

`scripts/run_data_load_check.py` and `run_code_change_check.py` stay as the
GitHub-specific entrypoints — they read `PR_BASE_SHA`/`PR_HEAD_SHA`, post the PR
summary comment, and push the updated results store back to `main`.
`run_check.py` shares the same reconciliation and agent code underneath; the
checks themselves behave identically. `code-change` defaults to
reconciliation + classification only, and takes `--run-actions` to also fire the
Confluence/Jira/Slack branch — so a container consumer gets the classification
and the audit row without needing those accounts configured.
