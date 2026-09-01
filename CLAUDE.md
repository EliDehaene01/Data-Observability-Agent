# CLAUDE.md — Project Brief

## What this is

`data-observability-agent`: an agentic system that verifies dbt views (transformation
outputs) stay accurate and complete relative to their source system (e.g. SAP-style
ERP). It reasons about *business logic*, not just raw diffs — distinguishing "the
numbers changed because the SQL intentionally changed" from "the numbers changed
because something broke."

Portfolio project, built vendor-agnostic and cheap (local DuckDB/Postgres, GitHub
Actions free tier, free-tier Confluence/Jira). Real SAP and real cloud infra are
future swap-ins, not MVP requirements.

## Two triggers, two jobs — do not conflate them

1. **Data-load validation** (scheduled, e.g. nightly)
   Business logic hasn't changed. Pure data-integrity check: aggregate diffs (row
   counts, sums, key metrics) over a rolling window + row-level sampling.
   **Deterministic. No LLM call belongs in this path.**

2. **Code-change validation** (triggered on dbt PR/deploy)
   Business logic *did* change. The agent reads the SQL diff + reconciliation results
   + PR description and classifies whether a discrepancy is `expected`,
   `needs_review`, or `anomaly`. **This is the only path that invokes an LLM for
   *validation*.** (The same PR trigger also runs a second, unrelated LLM call
   that writes plain-English model documentation — see the two-LLM-uses rule
   below. It classifies nothing and gates nothing.)

   This validation job is a **required status check** on `main`: it fails the
   PR when `final_classification` is `needs_review` or `anomaly` (anything but a
   clean `expected`). The `results_store` audit row is always written *before*
   the check fails, so a blocked or never-merged PR still leaves history.

If you're ever unsure which path a piece of logic belongs in, ask before writing it —
this separation is the core design decision of the project, not an implementation
detail.

## Hard architectural rules

- **`reconciliation/` never calls an LLM.** It's plain Python + SQL — row counts,
  sums, rolling-window diffs, sampled row comparisons. Output is structured
  (pydantic) JSON. If you find yourself wanting to add reasoning here, that logic
  belongs in `agent/` instead, consuming `reconciliation/`'s output.
- **`agent/` only ever consumes structured output.** It never re-derives or
  re-computes reconciliation results itself — it reasons over what
  `reconciliation/` already produced.
- **Every external system goes through a connector interface first.** Source DB,
  target warehouse, docs, ticketing — each has a `base.py` defining the interface.
  Concrete implementations (Postgres, DuckDB, Confluence, Jira, Slack) are swappable
  without touching `reconciliation/` or `agent/`. When adding a new integration,
  add a new class implementing the existing interface — don't special-case a
  connector type into calling code.
- **LLM calls use forced structured (JSON-schema) output.** The `classify_discrepancy`
  node's output must be one of `expected | needs_review | anomaly` plus supporting
  fields — never free text that a downstream step has to parse.
- **There are exactly two deliberate LLM uses in this codebase, and they are
  separate nodes with separate scopes.** This was previously scoped to
  `classify_discrepancy` only:
  1. **`agent/nodes/classify_discrepancy.py`** — *validation*. Reasons about
     whether a reconciliation discrepancy is `expected | needs_review | anomaly`.
     Runs inside `agent/graph.py`. Its output gates PR merges.
  2. **`agent/nodes/summarize_model_logic.py`** — *documentation*. Takes a
     changed dbt model's compiled SQL and returns a plain-English summary of the
     business logic it performs, for the per-model Confluence pages. **Not** in
     `agent/graph.py`; never sees reconciliation results; gates nothing.
  Both force structured (JSON-schema) tool-use output. Structural facts about a
  model — its source tables, `ref()`/`source()` lineage, column names/types —
  are **never** LLM-inferred: they come straight from dbt's own `manifest.json`
  + `catalog.json` (`model_docs/manifest.py`), which are authoritative and
  hallucination-free. The LLM only ever describes *logic*. Don't add a third
  LLM use, or fold `summarize_model_logic` into `classify_discrepancy`, without
  a concrete reason — the value of the split is that validation stays a small,
  auditable surface.
- **Confluence pages are always nested, never flat.** `connectors/docs/confluence.py`'s
  `publish_page` ensures a parent/section page exists (creating it once if
  missing, reusing it forever after — it checks first) and publishes the real
  page as a *child* via the `ancestors` parameter. Reconciliation reports go
  under **"Reconciliation Updates"** (the connector default); per-model
  documentation goes under **"Model Documentation"**. Publishing is an upsert:
  a page with the same title is updated in place (version bumped), not
  duplicated — so per-model doc pages have stable titles and reconciliation
  reports have unique per-run titles.
- **Environment awareness (dev/qa/prd) lives in `config/environments.yml` as data**,
  not as branching code. Thresholds and auto-action permissions differ per
  environment; the code path is identical across environments and just reads config.
  prd is intentionally strict — low tolerance for auto-resolution, tends to escalate
  rather than auto-act.
- **The dashboard only ever reads from `results_store/`, never from live
  reconciliation output.** Every trigger workflow writes its results to the store
  first; the `ReportingConnector` implementation queries that store. This keeps the
  dashboard decoupled from whichever reconciliation/agent internals change later.

## LangGraph flow (agent/graph.py)

```
fetch_reconciliation_results → analyze_diff → classify_discrepancy (LLM, structured output)
  → [conditional branch on classification]
      expected      → draft_confluence_update → publish_docs
      needs_review  → draft_summary → post_slack_notification
      anomaly       → create_jira_ticket → notify_team
```

`summarize_model_logic` is **not** a node in this graph — the per-model
documentation pipeline (`scripts/generate_model_docs.py`) runs it directly,
outside LangGraph, on the same PR trigger. Don't wire it into `graph.py`.

`post_slack_notification` (`connectors/ticketing/slack.py`) is a one-way
notification only — **not** a human-in-the-loop approval step. Real
interactive approval would need a hosted callback endpoint to receive a
human's response, which this project doesn't have; that's a known,
deliberate MVP limitation, not an oversight. Don't rename it back to
anything implying "for_approval" until that callback actually exists.

`classify_discrepancy`'s LLM call only ever produces a raw
`(classification, confidence, reasoning, pr_claims_no_impact)` tuple.
`_apply_decision_logic` in `agent/nodes/classify_discrepancy.py` then
decides the *final* classification the graph branches on, via two rules,
applied in order:

1. **Confidence gating.** An `expected` classification is only trusted at
   or above `config/environments.yml`'s `confidence_threshold` for the
   current environment; below it, downgrade to `needs_review`.
   `needs_review`/`anomaly` are never gated on confidence.
2. **PR-honesty override**, independent of confidence entirely. If
   `pr_claims_no_impact` is true AND `diff_touched_tables` is non-empty
   (the diff actually touches a table involved in the flagged
   discrepancy) AND a reconciliation result actually exceeded its
   threshold, force `needs_review` regardless of the LLM's raw
   classification or confidence — this can even override a raw
   `anomaly`. The `diff_touched_tables` condition matters: a genuinely
   no-op change (e.g. a comment) on a table unrelated to the divergence
   correctly earns `pr_claims_no_impact=True` too, but that's irrelevant
   information, not PR dishonesty — without this guard the override
   wrongly downgraded the untouched-table `anomaly` case (caught by a
   regression check; don't remove the guard without re-checking that
   case). This rule exists because a live diagnostic (10 repeated runs of
   the same PR-claims-no-impact-but-diff-disagrees scenario) found the
   model reliably scored confidence at 0.72–0.75 — above a 0.6 dev
   threshold — in 8/10 runs, even while its own `reasoning` text said the
   PR's claim was "contradicted"/"factually wrong". Confidence alone
   measures whether the diff explains a metric's direction/magnitude; it
   does not measure whether the PR's own narrative about impact is
   honest — those are two different judgments, which is why they're now
   two different fields with two independent deterministic checks. Don't
   collapse this back into rule 1 or drop it without re-running that
   diagnostic first.

State object carries: reconciliation results, diff context, classification, and
drafted outputs across nodes. Keep it in `agent/state.py` as the single shared
schema — don't let individual nodes invent their own ad hoc state shapes.

## Repo structure conventions

- `dbt_project/` — the codebase being monitored, not the monitoring system itself.
  Models are organized in three layers, not the generic staging/marts split:
  - `models/landing/` — 1:1 with source (`landing_vbak`, `landing_vbap`). Cleaned/typed
    only. No filtering, no business logic. Any mismatch here is a bug, not a design
    decision.
  - `models/prep/` — where actual business logic lives (`prep_sales_orders`).
    Intentional source-to-target divergence is introduced here (e.g. excluding
    cancelled orders, flagging incomplete ones) and should be clearly commented,
    since this is exactly what `classify_discrepancy` needs to reason about later.
  - `models/serve/` — business-friendly final layer (`serve_sales_orders`,
    `serve_monthly_revenue`). Renames/aggregates only — no *new* logic should ever
    sneak in here. If you're tempted to add a business rule in `serve/`, it belongs
    in `prep/` instead.
- `mock_erp/` — synthetic SAP-style source data (e.g. VBAK/VBAP-style tables).
- `connectors/{source,target,docs,ticketing,reporting}/` — abstraction layer,
  `base.py` first. `reporting/` follows the same pattern as `docs/`/`ticketing/` —
  the MVP implementation is a static HTML dashboard published via GitHub Pages, but
  the interface should allow swapping in something else (e.g. Power BI, Metabase)
  later without touching the results store or reconciliation engine.
- `reconciliation/` — deterministic engine only.
- `model_docs/` — per-model documentation pipeline. `manifest.py` reads dbt's
  `manifest.json` + `catalog.json` (deterministic; lineage + columns/types) and
  parses the changed-models list out of a unified diff; `render.py` builds
  Confluence storage-format page bodies; `models.py` holds the pydantic
  payloads. The one LLM call it needs (`summarize_model_logic`) lives in
  `agent/nodes/`, not here. Orchestrated by `scripts/generate_model_docs.py`.
- `results_store/` — append-only persistence layer that both trigger workflows write
  to. One row per `ReconciliationResult` (run_id, environment, trigger_type,
  run_timestamp, check_type, table, metric, source/target values, diff_pct,
  threshold, status), plus four columns only ever populated for
  `trigger_type="code_change"` runs: `final_classification`, `confidence`,
  `pr_claims_no_impact`, `downgraded` — `on_data_load.yml` runs leave these
  `NULL`, since data-load validation never classifies anything. These four were
  added after the schema already had real rows in it; `writer.py`'s
  `ALTER TABLE ADD COLUMN IF NOT EXISTS` keeps that backward-compatible, so
  don't reintroduce a hard schema requirement that breaks reading old rows.
  The dashboard reads from here; it never queries reconciliation output or
  agent state directly.
- `powerbi/` — local Power BI companion: a committed PBIP / TMDL project
  (text) over CSVs that `scripts/export_results_for_powerbi.py` dumps from the
  results store (via `results_store.reader`, same boundary rule as the
  dashboard — no reconciliation/agent code). `powerbi/data/` (the CSVs) is
  gitignored. It's a **manual, local** artifact — Power BI Desktop has no
  headless mode, so it is deliberately not in CI (see `docs/powerbi.md`); same
  class of MVP limitation as `post_slack_notification` being notify-only.
- `agent/` — LangGraph reasoning layer only.
- `config/` — environment thresholds/rules as YAML, secrets loading via `.env`.
- `.github/workflows/` — three entry points: `on_data_load.yml` (schedule),
  `on_dbt_change.yml` (PR-triggered, plus a `workflow_dispatch` manual
  fallback) — both write to `results_store/` after running, straight to `main`
  regardless of which branch triggered them (a PR that never merges must not
  silently lose its audit trail) — and `publish_dashboard.yml` (push to `main`,
  path-filtered to `results_store/results.duckdb`), which regenerates and
  redeploys the dashboard after either of the other two commits.
  - `on_dbt_change.yml`'s `dbt-change-check` **job** is the required status
    check: its final step exits non-zero when the classification is blocking
    (`needs_review`/`anomaly`), after the results commit. Enabling branch
    protection to *require* that check is a repo-settings change the owner must
    make by hand (see README.md) — code can't.
  - The `workflow_dispatch` inputs (`sql_diff`, `pr_description`, `environment`)
    let the whole path run without a real PR. In that mode the scripts read the
    inputs directly, the real PR comment is skipped (logged instead), and the
    results-store commit to `main` is skipped so manual tests don't pollute the
    audit trail / dashboard.
  - The same workflow regenerates per-model Confluence docs (`dbt docs generate`
    → `scripts/generate_model_docs.py`) for every changed model. That step is
    `continue-on-error` — documentation must not block a PR.

## What NOT to do

- Don't add LLM calls inside `reconciliation/`.
- Don't let `summarize_model_logic` (or any LLM) infer a model's source tables,
  lineage, or columns — that's `manifest.json` / `catalog.json`'s job.
- Don't create flat top-level Confluence pages, and don't add a third LLM use,
  without a concrete reason.
- Don't hardcode dev/qa/prd behavior differences in Python — that belongs in
  `config/environments.yml`.
- Don't let a connector's implementation details leak into `reconciliation/` or
  `agent/` — those layers should only ever talk to the `base.py` interface.
- Don't have LLM nodes return free-text that other code parses — force structured
  output.
- Don't build out real SAP, Confluence, or Jira integrations speculatively — stub
  connectors are fine until there's a concrete reason to go further.
- Don't try to wire the `powerbi/` companion into CI or the container — Power BI
  Desktop can't run headless. The export script can run anywhere; opening/refreshing
  the PBIP is a manual local step, on purpose.
