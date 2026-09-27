"""One-time cleanup after the 2026-09-27 aggregate-checks fix (see
reconciliation/aggregate_checks.py's module docstring): archive every row
currently in the results store to a dated CSV, then reset the live store
to an empty `results` table with the same schema, so the dashboard starts
fresh from the corrected logic.

This is the one sanctioned exception to results_store's append-only rule
(results_store/writer.py): nothing is lost, because the archive holds
every row and column, and the script verifies that before resetting
anything.

The store lives on the data-results branch, not main (see CLAUDE.md), so
fetch it first and push the reset file back afterwards:

    git show origin/data-results:results_store/results.duckdb > results_store/results.duckdb
    python scripts/archive_and_reset_results.py
    # commit results_store/archive/*.csv to main (via PR), and the reset
    # results_store/results.duckdb to data-results

Usage: python scripts/archive_and_reset_results.py [--db-path PATH]
           [--archive-dir DIR] [--label pre-fix-YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from results_store.writer import _CREATE_TABLE_SQL, DEFAULT_DB_PATH  # noqa: E402

DEFAULT_ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "results_store" / "archive"


def _sql_str(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def archive(db_path: Path, archive_path: Path) -> int:
    """Export every row of `results` to `archive_path` (CSV with header, all
    columns, deterministic order), then read it back with the original
    column types and require an exact match both ways. Returns the row
    count."""
    if archive_path.exists():
        raise SystemExit(f"{archive_path} already exists -- refusing to overwrite an archive.")
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        n_rows = conn.execute("select count(*) from results").fetchone()[0]
        conn.execute(
            f"""copy (select * from results order by run_timestamp, run_id, check_type, "table", metric)
                to {_sql_str(archive_path.as_posix())} (header, delimiter ',')"""
        )
        types = {name: dtype for name, dtype, *_ in conn.execute("describe results").fetchall()}
        columns_sql = ", ".join(f"{_sql_str(name)}: {_sql_str(dtype)}" for name, dtype in types.items())
        read_back = (
            f"read_csv({_sql_str(archive_path.as_posix())}, header = true, "
            f"auto_detect = false, columns = {{{columns_sql}}})"
        )
        missing = conn.execute(f"select count(*) from (select * from results except all select * from {read_back})").fetchone()[0]
        extra = conn.execute(f"select count(*) from (select * from {read_back} except all select * from results)").fetchone()[0]
    finally:
        conn.close()

    if missing or extra:
        raise SystemExit(
            f"Archive verification failed ({missing} rows missing, {extra} unexpected) -- "
            f"store left untouched; inspect {archive_path}."
        )
    return n_rows


def reset(db_path: Path) -> None:
    """Replace the store with a brand-new file holding an empty `results`
    table (writer.py's own DDL). A new file rather than DELETE, so none of
    the old rows linger in unreclaimed DuckDB blocks."""
    with duckdb.connect(str(db_path), read_only=True) as old:
        old_schema = old.execute("describe results").fetchall()

    fresh_path = db_path.with_suffix(".reset.duckdb")
    fresh_path.unlink(missing_ok=True)
    with duckdb.connect(str(fresh_path)) as fresh:
        fresh.execute(_CREATE_TABLE_SQL)
        new_schema = fresh.execute("describe results").fetchall()
    if new_schema != old_schema:
        fresh_path.unlink()
        raise SystemExit(f"Schema mismatch, store left untouched:\n old={old_schema}\n new={new_schema}")

    fresh_path.replace(db_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--label", default=f"pre-fix-{date.today().isoformat()}")
    args = parser.parse_args()

    if not args.db_path.exists():
        raise SystemExit(f"{args.db_path} not found -- fetch it from origin/data-results first.")

    archive_path = args.archive_dir / f"{args.label}.csv"
    n_rows = archive(args.db_path, archive_path)
    print(f"Archived {n_rows} rows to {archive_path} (verified identical on read-back).")

    reset(args.db_path)
    with duckdb.connect(str(args.db_path), read_only=True) as conn:
        remaining = conn.execute("select count(*) from results").fetchone()[0]
    print(f"Reset {args.db_path}: results table now has {remaining} rows.")


if __name__ == "__main__":
    main()
