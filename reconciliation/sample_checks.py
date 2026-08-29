"""Deterministic row-level reconciliation: sample N source records, look up
the matching target record by key, and compare column values. No LLM code
belongs in this file (see CLAUDE.md).

A sampled record counts as a match only if the target has a corresponding
row (matched by key_columns) AND every compare_columns value is equal. A
record whose target row is missing entirely (e.g. because its order was
cancelled and dbt's prep_sales_orders excludes it -- see
dbt_project/models/prep/prep_sales_orders.sql) counts as a mismatch, same
as a record with a differing value: this module reports what it finds and
leaves interpretation to the configured threshold and, later, agent/'s
classification.

One ReconciliationResult is produced per table pair: source_value is the
number of records sampled, target_value is the number that matched, and
diff_pct is therefore the sampled mismatch rate -- compared against
config/environments.yml's sample_mismatch_pct for the given environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from connectors.source.base import SourceConnector
from connectors.target.base import TargetConnector
from reconciliation.models import ReconciliationResult

CONFIG_PATH = Path(__file__).parent.parent / "config" / "environments.yml"

# Each entry: sample source_table's rows, match them into target_table by
# key_columns, and compare compare_columns. column_mapping translates a
# source column name to its target column name when the target renames it
# (e.g. serve_sales_orders's business-friendly names) -- None means the
# names are identical on both sides.
TABLE_PAIRS = [
    {
        "source_table": "vbap",
        "target_table": "prep_sales_orders",
        "key_columns": ["order_id", "item_id"],
        "compare_columns": ["material_id", "quantity", "net_value"],
        "column_mapping": None,
    },
    {
        "source_table": "vbap",
        "target_table": "serve_sales_orders",
        "key_columns": ["order_id", "item_id"],
        "compare_columns": ["material_id", "quantity", "net_value"],
        "column_mapping": {
            "order_id": "sales_order_id",
            "item_id": "line_item_number",
            "material_id": "product_id",
        },
    },
]


def _load_thresholds(environment: str) -> dict[str, float]:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    return config["environments"][environment]["thresholds"]


def _pct_diff(source_value: float, target_value: float) -> float:
    if source_value == 0:
        return 0.0 if target_value == 0 else 100.0
    return abs(target_value - source_value) / abs(source_value) * 100.0


def _target_col(column: str, column_mapping: dict[str, str] | None) -> str:
    return (column_mapping or {}).get(column, column)


def run_sample_check(
    source: SourceConnector,
    target: TargetConnector,
    source_table: str,
    target_table: str,
    key_columns: list[str],
    compare_columns: list[str],
    environment: str,
    n: int = 50,
    column_mapping: dict[str, str] | None = None,
) -> ReconciliationResult:
    """Sample `n` rows from source_table, match each into target_table by
    key_columns, and compare compare_columns for equality."""
    thresholds = _load_thresholds(environment)
    run_timestamp = datetime.now(timezone.utc)

    sampled_rows = source.sample_rows(source_table, n)
    n_matched = 0

    for source_row in sampled_rows:
        target_filters = {
            _target_col(col, column_mapping): source_row[col] for col in key_columns
        }
        target_rows = target.sample_rows(target_table, 1, filters=target_filters)
        if not target_rows:
            continue  # missing in target -- a mismatch, not skipped

        target_row = target_rows[0]
        if all(
            source_row[col] == target_row.get(_target_col(col, column_mapping))
            for col in compare_columns
        ):
            n_matched += 1

    source_value = float(len(sampled_rows))
    target_value = float(n_matched)
    diff_pct = _pct_diff(source_value, target_value)
    threshold = thresholds["sample_mismatch_pct"]
    status = "flag" if diff_pct > threshold else "pass"

    return ReconciliationResult(
        check_type="sample",
        table=f"{source_table} -> {target_table}",
        metric="sample_mismatch_pct",
        source_value=source_value,
        target_value=target_value,
        diff_pct=diff_pct,
        threshold=threshold,
        status=status,
        environment=environment,
        run_timestamp=run_timestamp,
    )


def run_sample_checks(
    source: SourceConnector,
    target: TargetConnector,
    environment: str,
    n: int = 50,
) -> list[ReconciliationResult]:
    """Sample checks for every pair in TABLE_PAIRS."""
    return [
        run_sample_check(source, target, environment=environment, n=n, **pair)
        for pair in TABLE_PAIRS
    ]
