"""Read-only queries against results_store/results.duckdb, for the
dashboard (not built yet) to consume. Deliberately no aggregation or
analysis logic here -- just querying; that belongs in the dashboard layer
per CLAUDE.md. No LLM code either.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from results_store.writer import DEFAULT_DB_PATH


def _connect(db_path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path or DEFAULT_DB_PATH), read_only=True)


def _rows_as_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_recent_runs(
    environment: str | None = None,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """The `limit` most recent runs' metadata (one row per run_id), newest first."""
    conn = _connect(db_path)
    try:
        where_sql = "where environment = ?" if environment is not None else ""
        params: list[Any] = [environment] if environment is not None else []
        params.append(limit)
        cursor = conn.execute(
            f"""
            select distinct run_id, environment, trigger_type, run_timestamp
            from results
            {where_sql}
            order by run_timestamp desc
            limit ?
            """,
            params,
        )
        return _rows_as_dicts(cursor)
    finally:
        conn.close()


def get_run_by_id(run_id: str, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Every ReconciliationResult row belonging to `run_id`."""
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            'select * from results where run_id = ? order by check_type, "table", metric',
            [run_id],
        )
        return _rows_as_dicts(cursor)
    finally:
        conn.close()


def get_flagged_results(
    environment: str | None = None,
    since: datetime | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Every result row with status = 'flag', optionally scoped by
    environment and/or a run_timestamp cutoff."""
    conn = _connect(db_path)
    try:
        clauses = ["status = 'flag'"]
        params: list[Any] = []
        if environment is not None:
            clauses.append("environment = ?")
            params.append(environment)
        if since is not None:
            clauses.append("run_timestamp >= ?")
            params.append(since)
        where_sql = " and ".join(clauses)
        cursor = conn.execute(
            f"select * from results where {where_sql} order by run_timestamp desc",
            params,
        )
        return _rows_as_dicts(cursor)
    finally:
        conn.close()
