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
    status        VARCHAR   NOT NULL,
    final_classification VARCHAR,
    confidence            DOUBLE,
    pr_claims_no_impact   BOOLEAN,
    downgraded            BOOLEAN
);
ALTER TABLE results ADD COLUMN IF NOT EXISTS final_classification VARCHAR;
ALTER TABLE results ADD COLUMN IF NOT EXISTS confidence DOUBLE;
ALTER TABLE results ADD COLUMN IF NOT EXISTS pr_claims_no_impact BOOLEAN;
ALTER TABLE results ADD COLUMN IF NOT EXISTS downgraded BOOLEAN;
"""
# The ALTER TABLE statements exist for backward compatibility: a results
# table created before this schema change already exists (CREATE TABLE IF
# NOT EXISTS is then a no-op), so the ALTERs bring it up to date --
# existing rows just get NULL for the four new columns, which is exactly
# right, since those columns are only ever populated for trigger_type=
# "code_change" runs anyway.

_INSERT_SQL = """
INSERT INTO results (
    run_id, environment, trigger_type, run_timestamp,
    check_type, "table", metric, source_value, target_value,
    diff_pct, threshold, status,
    final_classification, confidence, pr_claims_no_impact, downgraded
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def write_run(
    run: ReconciliationRun,
    db_path: str | Path | None = None,
    final_classification: str | None = None,
    confidence: float | None = None,
    pr_claims_no_impact: bool | None = None,
    downgraded: bool | None = None,
) -> str:
    """Append one row per ReconciliationResult in `run`. Generates and
    returns a run_id shared by every row written for this run (the model
    itself carries no run_id -- persistence is this module's concern, not
    reconciliation/'s).

    The four classification parameters are only meaningful for
    trigger_type="code_change" runs (agent/graph.py's output) -- leave them
    at their None default for trigger_type="data_load" runs, which never
    involve classification. Passed as plain scalars, not a
    ClassificationResult, so this module doesn't need to depend on agent/'s
    types."""
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
                final_classification,
                confidence,
                pr_claims_no_impact,
                downgraded,
            )
            for result in run.results
        ]
        if rows:
            conn.executemany(_INSERT_SQL, rows)
    finally:
        conn.close()
    return run_id
