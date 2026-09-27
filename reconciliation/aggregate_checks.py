"""Deterministic aggregate reconciliation: row counts and sum(net_value),
source vs target, over the full dataset (no rolling window -- the seed data
is static). No LLM code belongs in this file (see CLAUDE.md); it only ever
compares numbers and applies thresholds pulled from
config/environments.yml.

Source side queries the raw vbak/vbap tables via SourceConnector (Postgres);
target side queries the dbt models via TargetConnector (DuckDB). Two kinds
of pair, answering two different questions:

1. Landing checks (vbak -> landing_vbak, vbap -> landing_vbap), never
   filtered. landing/ is defined as a faithful 1:1 pass-through of the
   source (see dbt_project/models/landing/), so these should sit at ~0% on
   every healthy run. They are the checks that catch a genuine data-load
   problem -- a failed or partial load, duplicated rows, a connector bug --
   because no business rule can explain a landing-layer divergence.

2. Prep/serve checks (vbap -> prep_sales_orders / serve_sales_orders).
   prep_sales_orders permanently excludes cancelled orders (business rule 1
   in dbt_project/models/prep/prep_sales_orders.sql), a fixed ~17%
   structural divergence from raw vbap. Compared against the unfiltered
   source, these checks exceed every environment's threshold on every run,
   so they carry no signal and would hide a real problem underneath the
   permanent noise. So the source side is always filtered to the population
   prep is *supposed* to contain (PREP_SOURCE_FILTERS), making the
   comparison apples-to-apples: ~0% on a healthy run, and anything left
   over is unexpected.

   Both triggers use the same filtered population. On data-load that
   isolates real load problems; on code-change it means a PR only sees the
   divergence *it* introduces (e.g. a new exclusion rule in prep) rather
   than the permanent cancelled-order gap every PR used to inherit, which
   made unrelated PRs look like anomalies. Filtered pairs carry the label
   "vbap (excl. cancelled) -> ..."; agent/nodes/analyze_diff.py reads the
   table name back out of it.

Historical note: runs written before 2026-09-27 compared prep/serve against
the *unfiltered* source (label "vbap -> prep_sales_orders") and flagged on
every run -- data_load runs until the data-load fix, code_change runs until
this filter was extended to them. Those flags reflect the intentional
cancelled-order exclusion, not a real ongoing problem. The pre-fix history
is archived in results_store/archive/pre-fix-2026-09-27.csv.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from connectors.source.base import Filters, Lookup, SourceConnector
from connectors.target.base import TargetConnector
from reconciliation.models import ReconciliationResult

CONFIG_PATH = Path(__file__).parent.parent / "config" / "environments.yml"

# Source-side mirror of prep_sales_orders.sql's row filter: vbap items whose
# vbak header is not cancelled. Only business rule 1 excludes rows --
# rule 2 (incomplete orders) flags but keeps them, and in_process/completed
# orders are kept as-is. This deliberately duplicates the dbt rule on
# main: a PR that changes prep's filter will (rightly) flag the difference
# for classify_discrepancy, and once merged this must be updated to match,
# or every later run flags it too.
PREP_SOURCE_FILTERS: Filters = {
    "order_id": ("in", Lookup("vbak", "order_id", {"status": ("!=", "cancelled")})),
}
PREP_SOURCE_LABEL = "vbap (excl. cancelled)"

# Each entry: compare source_table (optionally filtered to the population
# the target is expected to contain) against target_table. sum_column is
# None when the table has no net_value to sum (vbak is headers only).
# Column names match exactly across vbap, landing_vbap, prep_sales_orders
# and serve_sales_orders (none renames net_value), so no mapping is needed.
TABLE_PAIRS = [
    {"source_table": "vbak", "target_table": "landing_vbak", "sum_column": None, "prep_rules": False},
    {"source_table": "vbap", "target_table": "landing_vbap", "sum_column": "net_value", "prep_rules": False},
    {"source_table": "vbap", "target_table": "prep_sales_orders", "sum_column": "net_value", "prep_rules": True},
    {"source_table": "vbap", "target_table": "serve_sales_orders", "sum_column": "net_value", "prep_rules": True},
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

    for pair in TABLE_PAIRS:
        source_table, target_table = pair["source_table"], pair["target_table"]
        filtered = pair["prep_rules"]
        source_filters = PREP_SOURCE_FILTERS if filtered else None
        source_label = PREP_SOURCE_LABEL if filtered else source_table
        table_label = f"{source_label} -> {target_table}"

        source_count = float(source.get_row_count(source_table, filters=source_filters))
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

        if pair["sum_column"] is None:
            continue
        source_sum = source.get_aggregate(source_table, pair["sum_column"], "sum", filters=source_filters)
        target_sum = target.get_aggregate(target_table, pair["sum_column"], "sum")
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
