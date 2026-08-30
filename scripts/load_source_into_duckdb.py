"""EL bridge: copies mock_erp's vbak/vbap tables from the Postgres source
into the DuckDB warehouse file, so dbt's landing_vbak/landing_vbap models
(which read `source('mock_erp', ...)` as a plain table sitting in the same
DuckDB file) have something to read -- dbt-duckdb has no native way to read
a Postgres table directly.

A real deployment would use a proper EL tool (Fivetran, Airbyte, DuckDB's
postgres extension, etc.); this is the "vendor-agnostic and cheap" MVP
equivalent (see CLAUDE.md), reusing mock_erp/seed_data.py's existing
schema/insert helpers instead of introducing a new dependency.

Run this after seeding Postgres and before `dbt run`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import psycopg2

from mock_erp.seed_data import _insert_rows, create_schema

TABLES = {
    "vbak": ["order_id", "customer_id", "order_date", "status"],
    "vbap": ["order_id", "item_id", "material_id", "quantity", "net_value"],
}


def main() -> None:
    pg_conn = psycopg2.connect(os.environ["POSTGRES_CONNECTION_STRING"])
    duck_conn = duckdb.connect(os.environ.get("DUCKDB_PATH", "dev.duckdb"))
    try:
        create_schema(duck_conn)
        for table, columns in TABLES.items():
            with pg_conn.cursor() as cur:
                cur.execute(f'SELECT {", ".join(columns)} FROM {table}')
                rows = cur.fetchall()
            _insert_rows(duck_conn, table, columns, rows)
            print(f"Loaded {len(rows)} rows from Postgres into DuckDB table {table}")
    finally:
        pg_conn.close()
        duck_conn.close()


if __name__ == "__main__":
    main()
