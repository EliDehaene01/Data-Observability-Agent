"""Generate synthetic SAP-style sales order data (VBAK/VBAP) for mock_erp.

`seed()` is the reusable entry point: it takes an already-open DB-API-style
connection (anything exposing `.execute()`, e.g. a `duckdb.connect(...)` or
`psycopg2.connect(...)` object) and does not import a specific driver, so
the same code works whether the target is a DuckDB file or a Postgres
schema. Connection setup belongs to the caller (or, later, to a
`connectors/source/` implementation) -- not to this module.

Running this file directly (`python seed_data.py`) is a convenience CLI that
seeds either a local DuckDB file (default) or a Postgres database, selected
with `--engine`. Each engine gets its own thin driver-specific wrapper here
(DuckDB's connection already satisfies `Connection` directly; Postgres's
does not, since psycopg2 executes through a cursor, not the connection
itself) -- `seed()` itself still never imports a specific driver.
"""

from __future__ import annotations

import argparse
import os
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

STATUSES = ["completed", "completed", "completed", "in_process", "cancelled", "incomplete"]
# weighted list, not a uniform choice: completed dominates, cancelled/incomplete
# are the minority "noise" that later reconciliation checks need to see.

NUM_CUSTOMERS = 200
NUM_MATERIALS = 100
CUSTOMER_IDS = [f"CUST{n:05d}" for n in range(1, NUM_CUSTOMERS + 1)]
MATERIAL_IDS = [f"MAT{n:05d}" for n in range(1, NUM_MATERIALS + 1)]

FIRST_ORDER_ID = 4500000  # SAP VBELN numbers for orders conventionally start around here


class Connection(Protocol):
    def execute(self, sql: str) -> Any: ...


@dataclass
class SeedResult:
    orders: int
    line_items: int


def _random_date(start: date, end: date, rng: random.Random) -> date:
    span_days = (end - start).days
    return start + timedelta(days=rng.randint(0, span_days))


def generate_orders(
    num_orders: int = 3000,
    start_date: date = date(2026, 1, 1),
    end_date: date = date(2026, 4, 30),
    rng_seed: int = 42,
) -> tuple[list[tuple], list[tuple]]:
    """Build header (vbak) and line-item (vbap) rows in memory.

    Returns (headers, items) as lists of plain tuples matching the column
    order in schema.sql, so callers can insert them however they like.
    """
    rng = random.Random(rng_seed)
    headers: list[tuple] = []
    items: list[tuple] = []

    for i in range(num_orders):
        order_id = FIRST_ORDER_ID + i
        order_date = _random_date(start_date, end_date, rng)
        status = rng.choice(STATUSES)
        customer_id = rng.choice(CUSTOMER_IDS)
        headers.append((order_id, customer_id, order_date, status))

        # incomplete orders realistically have fewer line items entered so far
        max_items = 2 if status == "incomplete" else 6
        num_items = rng.randint(1, max_items)
        for item_id in range(1, num_items + 1):
            material_id = rng.choice(MATERIAL_IDS)
            quantity = rng.randint(1, 50)
            unit_value = rng.uniform(5.0, 500.0)
            net_value = round(quantity * unit_value, 2)
            items.append((order_id, item_id, material_id, quantity, net_value))

    return headers, items


def _sql_literal(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    return str(value)


def _insert_rows(conn: Connection, table: str, columns: list[str], rows: list[tuple], batch_size: int = 500) -> None:
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        values_sql = ",\n".join("(" + ", ".join(_sql_literal(v) for v in row) + ")" for row in batch)
        conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {values_sql}")


def create_schema(conn: Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text())


def seed(
    conn: Connection,
    num_orders: int = 3000,
    rng_seed: int = 42,
    create_tables: bool = True,
) -> SeedResult:
    """Create (if needed) and populate vbak/vbap on an open connection."""
    if create_tables:
        create_schema(conn)

    headers, items = generate_orders(num_orders=num_orders, rng_seed=rng_seed)
    _insert_rows(conn, "vbak", ["order_id", "customer_id", "order_date", "status"], headers)
    _insert_rows(conn, "vbap", ["order_id", "item_id", "material_id", "quantity", "net_value"], items)

    if hasattr(conn, "commit"):
        conn.commit()

    return SeedResult(orders=len(headers), line_items=len(items))


class _PsycopgExecuteAdapter:
    """Adapts a psycopg2 connection to the `Connection` protocol `seed()`
    expects. psycopg2 has no `.execute()` on the connection itself -- every
    statement needs a cursor -- so this wrapper hides that one difference.
    """

    def __init__(self, psycopg2_conn: Any) -> None:
        self._conn = psycopg2_conn

    def execute(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def commit(self) -> None:
        self._conn.commit()


def _seed_duckdb(duckdb_path: str, num_orders: int, rng_seed: int) -> SeedResult:
    import duckdb  # lazy: only this branch is DuckDB-specific

    conn = duckdb.connect(duckdb_path)
    try:
        return seed(conn, num_orders=num_orders, rng_seed=rng_seed)
    finally:
        conn.close()


def _seed_postgres(connection_string: str, num_orders: int, rng_seed: int) -> SeedResult:
    import psycopg2  # lazy: only this branch is Postgres-specific

    conn = psycopg2.connect(connection_string)
    try:
        result = seed(_PsycopgExecuteAdapter(conn), num_orders=num_orders, rng_seed=rng_seed)
    finally:
        conn.close()
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["duckdb", "postgres"], default="duckdb")
    parser.add_argument(
        "--duckdb-path",
        default=os.environ.get("DUCKDB_PATH", "dev.duckdb"),
        help="Path to the DuckDB file to seed when --engine=duckdb (default: $DUCKDB_PATH or dev.duckdb)",
    )
    parser.add_argument(
        "--postgres-connection-string",
        default=os.environ.get("POSTGRES_CONNECTION_STRING"),
        help="Connection string to seed when --engine=postgres (default: $POSTGRES_CONNECTION_STRING)",
    )
    parser.add_argument("--num-orders", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if args.engine == "duckdb":
        result = _seed_duckdb(args.duckdb_path, args.num_orders, args.seed)
        target = args.duckdb_path
    else:
        if not args.postgres_connection_string:
            parser.error("--postgres-connection-string or $POSTGRES_CONNECTION_STRING is required for --engine=postgres")
        result = _seed_postgres(args.postgres_connection_string, args.num_orders, args.seed)
        target = "Postgres"

    print(f"Seeded {result.orders} orders / {result.line_items} line items into {target}")


if __name__ == "__main__":
    _main()
