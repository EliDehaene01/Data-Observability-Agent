# data-observability-agent — Build Checklist

- [x] **1. Environment setup**
  Python, git, uv, dbt-core/dbt-duckdb installed. GitHub repo created and connected.
  `config/environments.yml`, `.env`, `CLAUDE.md` in place.

- [x] **2. Mock ERP source data**
  `mock_erp/schema.sql` (VBAK/VBAP-styled) + `mock_erp/seed_data.py` + `mock_erp/README.md`.
  Verified end-to-end against DuckDB.

- [ ] **3. dbt project with real business logic**
  `models/landing/` (1:1 with source: `landing_vbak`, `landing_vbap`) →
  `models/prep/` (`prep_sales_orders` — excludes cancelled orders, flags
  incomplete orders) → `models/serve/` (`serve_sales_orders`,
  `serve_monthly_revenue` — business-friendly names, no new logic).
  dbt tests for keys, referential integrity, and no-cancelled-orders-leak-through.
  Must pass `dbt run` + `dbt test` against DuckDB.

- [ ] **4. Connector abstraction layer**
  `connectors/source/base.py`, `connectors/target/base.py` interfaces + Postgres/DuckDB
  implementations.

- [ ] **5. Deterministic reconciliation engine**
  `reconciliation/aggregate_checks.py`, `sample_checks.py`, `models.py` (structured
  JSON output). No LLM in this layer.

- [ ] **6. Persistent results store**
  Append-only table (DuckDB/Postgres) that every reconciliation run writes a row
  into — run timestamp, trigger type, environment, pass/fail, discrepancy summary.
  Both trigger workflows (below) write here. This is what the dashboard reads from;
  without it results only ever exist as one-off JSON per run.

- [ ] **7. GitHub Actions trigger #1 — scheduled data-load check**
  `.github/workflows/on_data_load.yml`. Writes results to the results store.

- [ ] **8. LangGraph reasoning agent**
  `agent/state.py`, `agent/graph.py` — fetch → analyze → classify_discrepancy (LLM,
  structured output) → branch (expected / needs_review / anomaly).

- [ ] **9. Docs, ticketing, and Slack connectors**
  `connectors/docs/confluence.py`, `connectors/ticketing/jira.py`,
  `connectors/ticketing/slack.py`. Create free-tier accounts at this point.

- [ ] **10. GitHub Actions trigger #2 — PR-triggered code-change check**
  `.github/workflows/on_dbt_change.yml`, running the full LangGraph flow. Writes
  results to the results store.

- [ ] **11. HTML dashboard, published via GitHub Pages**
  Reads from the results store; shows pass/fail history for both data-load and
  code-change checks, discrepancy trends, per-environment status. Generated as a
  static page and published via GitHub Pages — free, no new accounts, no hosting.
  Built behind a `ReportingConnector` interface (same pattern as docs/ticketing) so
  a different backend (e.g. Power BI, Metabase) can swap in later without touching
  the results store or reconciliation engine.

- [ ] **12. Tests and polish**
  `tests/test_reconciliation.py`, `test_connectors.py`, `test_agent_graph.py`, plus
  `docs/architecture.md`.

## Product / distribution (post-MVP)

- [ ] **13. Package as a Docker container**
  Bundle the app + dependencies (Python, dbt-core, dbt-duckdb, connectors) into a
  Docker image so it can run in any customer's infra/orchestration — not just GitHub
  Actions. Chosen over a GitHub App for broader enterprise compatibility, since most
  enterprises don't orchestrate this kind of workflow through GitHub itself. Customer
  points the container at their own config (`environments.yml` pattern) and
  credentials via environment variables. Helm chart is a possible later addition on
  top, only relevant if targeting Kubernetes-native customers specifically.
- [ ] **14. Validate demand before building further**
  Talk to contacts at SAP/dbt shops — confirm the problem framing resonates and
  surface their actual orchestration/deployment constraints (Docker vs. Kubernetes vs.
  something else), and whether Power BI (vs. the MVP's HTML dashboard) matters enough
  to prioritize as a real `ReportingConnector` implementation.
