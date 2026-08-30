"""Append-only persistence for ReconciliationRun output, in its own DuckDB
file (results_store/results.duckdb) -- separate from the dbt target
database (dev.duckdb) so results survive independently of it. No LLM code
belongs here (see CLAUDE.md); this only ever inserts rows, never updates or
deletes them.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import duckdb

from reconciliation.models import ReconciliationRun

DEFAULT_DB_PATH = Path(__file__).parent / "results.duckdb"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS results (
    run_id        VARCHAR   NOT NULL,
    environment   VARCHAR   NOT NULL,
    trigger_type  VARCHAR   NOT NULL,
    run_timestamp TIMESTAMP NOT NULL,
    check_type    VARCHAR   NOT NULL,
    "table"       VARCHAR   NOT NULL,
    metric        VARCHAR   NOT NULL,
    source_value  DOUBLE    NOT NULL,
    target_value  DOUBLE    NOT NULL,
    diff_pct      DOUBLE    NOT NULL,
    threshold     DOUBLE    NOT NULL,
    status        VARCHAR   NOT NULL
)
"""

_INSERT_SQL = """
INSERT INTO results (
    run_id, environment, trigger_type, run_timestamp,
    check_type, "table", metric, source_value, target_value,
    diff_pct, threshold, status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def write_run(run: ReconciliationRun, db_path: str | Path | None = None) -> str:
    """Append one row per ReconciliationResult in `run`. Generates and
    returns a run_id shared by every row written for this run (the model
    itself carries no run_id -- persistence is this module's concern, not
    reconciliation/'s)."""
    run_id = str(uuid.uuid4())
    conn = duckdb.connect(str(db_path or DEFAULT_DB_PATH))
    try:
        conn.execute(_CREATE_TABLE_SQL)
        rows = [
            (
                run_id,
                run.environment,
                run.trigger_type,
                run.run_timestamp,
                result.check_type,
                result.table,
                result.metric,
                result.source_value,
                result.target_value,
                result.diff_pct,
                result.threshold,
                result.status,
            )
            for result in run.results
        ]
        if rows:
            conn.executemany(_INSERT_SQL, rows)
    finally:
        conn.close()
    return run_id
