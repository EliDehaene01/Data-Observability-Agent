# tests/

Formalizes what was verified ad hoc, by hand, across earlier build phases
into a real pytest suite.

## What's in here

- **`test_reconciliation.py`** — `reconciliation/aggregate_checks.py` and
  `sample_checks.py` against real Postgres/DuckDB connectors. No mocking.
- **`test_connectors.py`** — `PostgresSourceConnector` and
  `DuckDBTargetConnector` against real local databases. No mocking.
- **`test_agent_graph.py`** — `classify_discrepancy`'s decision logic
  (confidence gating + the PR-honesty override). The Anthropic API call
  itself is mocked by default; a small number of tests marked
  `@pytest.mark.live` call the real model instead (see below).

## Prerequisites

The reconciliation/connector tests are integration tests against **real**
local infrastructure, same as every manual verification done throughout
this project:

- A local Postgres instance reachable via `POSTGRES_CONNECTION_STRING`
  (see `.env`), with the `mock_erp` database created.
- `DUCKDB_PATH` set (see `.env`) -- the test session builds its own
  isolated DuckDB target in a temp file, so this only needs to be *set*,
  not point anywhere meaningful.

If the `vbak`/`vbap` tables in Postgres are empty or missing, the test
session seeds them automatically (`mock_erp.seed_data.seed`, same seed as
`mock_erp/seed_data.py --engine postgres`). If they already have data, the
suite reuses it as-is -- it never drops or resets existing rows.

## Running the default (fast, mocked) suite

```bash
uv run pytest
```

This is what CI would run. It:

- Never calls the real Anthropic API (no `ANTHROPIC_API_KEY` required for
  `test_agent_graph.py` -- a fake key is set automatically, and
  `anthropic.Anthropic` is mocked).
- Does call the real local Postgres and DuckDB (this part is intentionally
  a real integration test, not mocked -- see CLAUDE.md's connector
  abstraction rule: the point is proving the actual SQL against actual
  engines, not a stand-in).
- Builds the DuckDB target once per session (a few seconds: seed check +
  EL + `dbt run`), then reuses it across all tests in
  `test_reconciliation.py`/`test_connectors.py`.

## Running everything, including live LLM sanity checks

```bash
uv run pytest --run-live
```

`@pytest.mark.live` tests call the real Anthropic API with real API usage
cost, and their assertions can occasionally fail on genuinely borderline
prompts even when nothing is broken -- live LLM output is not fully
deterministic (this is exactly the behavior the PR-honesty override in
`agent/nodes/classify_discrepancy.py` exists to guard against; see
`docs/architecture.md`). They're for manually sanity-checking after a
prompt or schema change, not for routine/CI runs -- that's why they're
skipped unless you explicitly opt in with `--run-live`.

To run only the live tests:

```bash
uv run pytest --run-live -m live
```

## Notes for extending this suite

- Don't hardcode magic numbers (e.g. "17.26%") that depend on the exact
  seed data volume/RNG seed -- compute the expected value independently
  from the same connectors under test (see
  `test_cancelled_order_divergence_matches_expected_magnitude` for the
  pattern), so the tests don't silently rot if the seed data changes.
- `reconciliation/` and `connectors/` tests must never call an LLM (see
  CLAUDE.md) -- if a test in those files needs mocking to avoid a real
  call, that's a sign the logic being tested has drifted into the wrong
  layer.
