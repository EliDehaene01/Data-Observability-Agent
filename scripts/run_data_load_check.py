"""CI entry point for the scheduled data-load validation trigger
(.github/workflows/on_data_load.yml). Pure deterministic reconciliation --
no LLM code, no LangGraph, per CLAUDE.md.

Runs both check types against POSTGRES_CONNECTION_STRING (source) and
DUCKDB_PATH (target, built by `dbt run` immediately before this script),
wraps the results in a ReconciliationRun (trigger_type="data_load"), and
appends it to results_store via write_run.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.source.postgres_source import PostgresSourceConnector
from connectors.target.duckdb_target import DuckDBTargetConnector
from reconciliation.aggregate_checks import run_aggregate_checks
from reconciliation.models import ReconciliationRun
from reconciliation.sample_checks import run_sample_checks
from results_store.writer import write_run

ENVIRONMENT = "dev"


def main() -> None:
    source = PostgresSourceConnector()
    target = DuckDBTargetConnector()
    try:
        results = run_aggregate_checks(source, target, ENVIRONMENT) + run_sample_checks(
            source, target, ENVIRONMENT
        )
    finally:
        source.close()
        target.close()

    run = ReconciliationRun(
        environment=ENVIRONMENT,
        run_timestamp=datetime.now(timezone.utc),
        trigger_type="data_load",
        results=results,
    )
    run_id = write_run(run)

    flagged = [r for r in run.results if r.status == "flag"]
    print(f"Wrote run_id={run_id} with {len(run.results)} results ({len(flagged)} flagged).")
    for r in run.results:
        marker = "FLAG" if r.status == "flag" else "pass"
        print(
            f"  [{marker}] {r.check_type:9s} {r.table:30s} {r.metric:20s} "
            f"diff_pct={r.diff_pct:.2f} threshold={r.threshold:.2f}"
        )


if __name__ == "__main__":
    main()
