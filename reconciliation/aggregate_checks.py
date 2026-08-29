"""Deterministic aggregate reconciliation: row counts and sum(net_value),
source vs target, over the full dataset (no rolling window -- the seed data
is static). No LLM code belongs in this file (see CLAUDE.md); it only ever
compares numbers and applies thresholds pulled from
config/environments.yml.

Source side queries `vbap` directly via SourceConnector (Postgres). This is
deliberately not `landing_vbap`: landing_vbap is dbt's 1:1, no-filtering
copy of vbap (see dbt_project/models/landing/landing_vbak.sql), so
querying the real source table gives the same numbers without routing a
"source-side" check through the target warehouse.

Target side queries prep_sales_orders and serve_sales_orders via
TargetConnector (DuckDB). Both intentionally exclude cancelled orders (see
dbt_project/models/prep/prep_sales_orders.sql) -- so these row counts and
sums are *expected* to diverge from the source. This module does not special
-case that: it reports the diff_pct like any other check and lets the
configured threshold (and, later, agent/'s classification) decide what it
means.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from connectors.source.base import SourceConnector
from connectors.target.base import TargetConnector
from reconciliation.models import ReconciliationResult

CONFIG_PATH = Path(__file__).parent.parent / "config" / "environments.yml"

# (source_table, target_table) pairs to compare. Column names match exactly
# across all three (vbap, prep_sales_orders, serve_sales_orders never
# renames net_value), so no column mapping is needed here.
TABLE_PAIRS = [
    ("vbap", "prep_sales_orders"),
    ("vbap", "serve_sales_orders"),
]


def _load_thresholds(environment: str) -> dict[str, float]:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    return config["environments"][environment]["thresholds"]


def _pct_diff(source_value: float, target_value: float) -> float:
    """Absolute percentage difference of target from source. 0/0 is "no
    divergence"; 0-vs-nonzero is treated as a full (100%) divergence rather
    than raising a ZeroDivisionError."""
    if source_value == 0:
        return 0.0 if target_value == 0 else 100.0
    return abs(target_value - source_value) / abs(source_value) * 100.0


def _build_result(
    check_type: str,
    table: str,
    metric: str,
    source_value: float,
    target_value: float,
    threshold: float,
    environment: str,
    run_timestamp: datetime,
) -> ReconciliationResult:
    diff_pct = _pct_diff(source_value, target_value)
    status = "flag" if diff_pct > threshold else "pass"
    return ReconciliationResult(
        check_type=check_type,
        table=table,
        metric=metric,
        source_value=source_value,
        target_value=target_value,
        diff_pct=diff_pct,
        threshold=threshold,
        status=status,
        environment=environment,
        run_timestamp=run_timestamp,
    )


def run_aggregate_checks(
    source: SourceConnector,
    target: TargetConnector,
    environment: str,
) -> list[ReconciliationResult]:
    """Row-count and sum(net_value) checks for every pair in TABLE_PAIRS,
    thresholded against config/environments.yml[environment]."""
    thresholds = _load_thresholds(environment)
    run_timestamp = datetime.now(timezone.utc)
    results: list[ReconciliationResult] = []

    for source_table, target_table in TABLE_PAIRS:
        table_label = f"{source_table} -> {target_table}"

        source_count = float(source.get_row_count(source_table))
        target_count = float(target.get_row_count(target_table))
        results.append(
            _build_result(
                "aggregate",
                table_label,
                "row_count",
                source_count,
                target_count,
                thresholds["row_count_diff_pct"],
                environment,
                run_timestamp,
            )
        )

        source_sum = source.get_aggregate(source_table, "net_value", "sum")
        target_sum = target.get_aggregate(target_table, "net_value", "sum")
        results.append(
            _build_result(
                "aggregate",
                table_label,
                "sum_net_value",
                source_sum,
                target_sum,
                thresholds["sum_diff_pct"],
                environment,
                run_timestamp,
            )
        )

    return results
