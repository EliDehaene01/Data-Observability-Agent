# mock_erp

Synthetic source-system data standing in for a real SAP-style ERP. This is
the "source of truth" that `dbt_project/` transforms and that
`reconciliation/` later checks the transformed output against — it is not
part of the monitoring system itself.

## Schema

Modeled loosely on two real SAP sales-document tables:

- **`vbak`** (sales order header, after SAP's VBAK) — one row per order:
  `order_id`, `customer_id`, `order_date`, `status`
  (`completed` / `in_process` / `cancelled` / `incomplete`).
- **`vbap`** (sales order line items, after SAP's VBAP) — one or more rows
  per order, foreign-keyed to `vbak.order_id`: `item_id` (line number within
  the order), `material_id`, `quantity`, `net_value`.

See `schema.sql` for full column definitions. It only uses plain ANSI SQL
types (`INTEGER`, `VARCHAR`, `DATE`, `DECIMAL`) and has no engine-specific
syntax, so it runs unmodified against either DuckDB or Postgres.

## Generating data

`seed_data.py` builds the rows in Python and inserts them through whatever
DB-API-style connection you pass it — it never imports a specific driver
itself, so the same code seeds a DuckDB file or a Postgres schema. That
connection is the caller's responsibility (or, later, a
`connectors/source/` implementation's).

Programmatic use:

```python
import duckdb
from mock_erp.seed_data import seed

conn = duckdb.connect("dev.duckdb")
result = seed(conn, num_orders=3000)
print(result)  # SeedResult(orders=3000, line_items=~10500)
```

`seed(conn, ...)` only requires `conn.execute(sql)`. A raw `psycopg2`
connection doesn't expose that directly (every statement needs a cursor),
so wrap it first with the adapter `seed_data.py` also provides:

```python
import psycopg2
from mock_erp.seed_data import seed, _PsycopgExecuteAdapter

conn = psycopg2.connect("postgresql://...")
result = seed(_PsycopgExecuteAdapter(conn), num_orders=3000)
conn.commit()
```

Standalone CLI, either engine:

```bash
# DuckDB (default) -- defaults to $DUCKDB_PATH if --duckdb-path is omitted
python mock_erp/seed_data.py --duckdb-path dev.duckdb --num-orders 3000

# Postgres -- defaults to $POSTGRES_CONNECTION_STRING if omitted
python mock_erp/seed_data.py --engine postgres --num-orders 3000
```

## Data characteristics

- A few thousand header rows by default, each with 1-6 line items
  (1-2 for `incomplete` orders).
- Orders span a multi-month date range (default: 2026-01-01 to 2026-04-30).
- Status mix is weighted toward `completed`, with `in_process`,
  `cancelled`, and `incomplete` as the minority "noise" — this is what
  makes aggregate checks (row counts, sums) meaningful to test against
  once `reconciliation/` exists.
- Generation is seeded (`rng_seed`, default 42) for reproducible runs.
