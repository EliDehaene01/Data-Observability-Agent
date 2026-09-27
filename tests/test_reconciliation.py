"""Tests for reconciliation/aggregate_checks.py and sample_checks.py --
deterministic engine only, no LLM, no mocking of the connectors (real
Postgres/DuckDB, see conftest.py). Covers:
  - thresholds are actually read from config/environments.yml per environment
  - status flips pass -> flag exactly at the threshold boundary
  - prep/serve are compared against the filtered source population, and
    healthy data sits at 0% while simulated load failures still flag
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from reconciliation.aggregate_checks import _build_result, run_aggregate_checks
from reconciliation.sample_checks import run_sample_checks

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "environments.yml"


# -- Threshold wiring -----------------------------------------------------


@pytest.mark.parametrize("environment", ["dev", "qa", "prd"])
def test_aggregate_thresholds_read_from_environments_yml(environment, postgres_source, duckdb_target):
    config = yaml.safe_load(CONFIG_PATH.read_text())
    expected = config["environments"][environment]["thresholds"]

    results = run_aggregate_checks(postgres_source, duckdb_target, environment)

    row_count_results = [r for r in results if r.metric == "row_count"]
    sum_results = [r for r in results if r.metric == "sum_net_value"]
    assert row_count_results and sum_results
    assert all(r.threshold == expected["row_count_diff_pct"] for r in row_count_results)
    assert all(r.threshold == expected["sum_diff_pct"] for r in sum_results)
    assert all(r.environment == environment for r in results)


@pytest.mark.parametrize("environment", ["dev", "qa", "prd"])
def test_sample_threshold_read_from_environments_yml(environment, postgres_source, duckdb_target):
    config = yaml.safe_load(CONFIG_PATH.read_text())
    expected = config["environments"][environment]["thresholds"]["sample_mismatch_pct"]

    results = run_sample_checks(postgres_source, duckdb_target, environment, n=20)

    assert results
    assert all(r.threshold == expected for r in results)


def test_thresholds_actually_differ_across_environments(postgres_source, duckdb_target):
    """Not just "some threshold was set" -- prove dev/qa/prd give genuinely
    different numbers, so a test that hardcoded one value everywhere
    couldn't pass by accident."""
    dev_results = run_aggregate_checks(postgres_source, duckdb_target, "dev")
    prd_results = run_aggregate_checks(postgres_source, duckdb_target, "prd")

    dev_row_count = next(r for r in dev_results if r.metric == "row_count")
    prd_row_count = next(r for r in prd_results if r.metric == "row_count")

    assert dev_row_count.threshold != prd_row_count.threshold
    assert dev_row_count.threshold == 5.0
    assert prd_row_count.threshold == 0.5


# -- Status boundary --------------------------------------------------------


def _dummy_result(source_value, target_value, threshold):
    now = datetime.now(timezone.utc)
    return _build_result(
        "aggregate", "t -> u", "m", source_value, target_value, threshold, "dev", now
    )


def test_status_is_pass_exactly_at_threshold():
    # diff_pct == threshold: status = "flag" if diff_pct > threshold else
    # "pass" -- equal must land on "pass".
    result = _dummy_result(source_value=100.0, target_value=95.0, threshold=5.0)
    assert result.diff_pct == 5.0
    assert result.status == "pass"


def test_status_is_flag_just_over_threshold():
    result = _dummy_result(source_value=100.0, target_value=94.9, threshold=5.0)
    assert result.diff_pct == pytest.approx(5.1)
    assert result.status == "flag"


def test_status_is_pass_just_under_threshold():
    result = _dummy_result(source_value=100.0, target_value=95.1, threshold=5.0)
    assert result.diff_pct == pytest.approx(4.9)
    assert result.status == "pass"


# -- Source-side population filter ------------------------------------------


def test_filtered_source_population_is_vbap_minus_cancelled_order_items(postgres_source, duckdb_target):
    """PREP_SOURCE_FILTERS must select exactly the vbap items prep keeps:
    everything except items of cancelled orders. Computed independently
    (total minus the complementary lookup) rather than hardcoded, so this
    doesn't rot if the seed data changes."""
    from connectors.source.base import Lookup

    total_items = postgres_source.get_row_count("vbap")
    cancelled_items = postgres_source.get_row_count(
        "vbap", filters={"order_id": ("in", Lookup("vbak", "order_id", {"status": "cancelled"}))}
    )
    assert cancelled_items > 0, "seed data should include some cancelled orders"

    results = run_aggregate_checks(postgres_source, duckdb_target, "dev")
    prep_row_count = next(
        r for r in results
        if r.table == "vbap (excl. cancelled) -> prep_sales_orders" and r.metric == "row_count"
    )
    assert prep_row_count.source_value == total_items - cancelled_items


def test_serve_sales_orders_matches_prep_sales_orders_divergence(postgres_source, duckdb_target):
    """serve_sales_orders is a faithful rename of prep_sales_orders (see
    dbt_project/models/serve/serve_sales_orders.sql) -- its divergence from
    source should be identical."""
    results = run_aggregate_checks(postgres_source, duckdb_target, "dev")
    prep_result = next(
        r for r in results
        if r.table == "vbap (excl. cancelled) -> prep_sales_orders" and r.metric == "row_count"
    )
    serve_result = next(
        r for r in results
        if r.table == "vbap (excl. cancelled) -> serve_sales_orders" and r.metric == "row_count"
    )
    assert prep_result.diff_pct == serve_result.diff_pct


# -- Business-rule filtering (both triggers) -----------------------------------
# prep/serve are compared against the source population they're supposed to
# contain (cancelled orders excluded), and the unfiltered landing checks
# guard the load itself. See reconciliation/aggregate_checks.py's module
# docstring.


def test_landing_checks_show_no_divergence_on_healthy_run(postgres_source, duckdb_target):
    results = run_aggregate_checks(postgres_source, duckdb_target, "prd")
    landing = [r for r in results if "landing_" in r.table]
    assert {r.table for r in landing} == {"vbak -> landing_vbak", "vbap -> landing_vbap"}
    assert all(r.diff_pct == 0 and r.status == "pass" for r in landing)


def test_business_rule_filter_removes_cancelled_order_noise(postgres_source, duckdb_target):
    """Healthy data: every check passes even at prd's 0.5%/1% thresholds."""
    results = run_aggregate_checks(postgres_source, duckdb_target, "prd") + run_sample_checks(
        postgres_source, duckdb_target, "prd", n=50
    )

    assert all(r.status == "pass" for r in results), [r for r in results if r.status == "flag"]
    assert all(r.diff_pct == 0 for r in results)
    prep_tables = {r.table for r in results if "landing_" not in r.table}
    assert prep_tables == {
        "vbap (excl. cancelled) -> prep_sales_orders",
        "vbap (excl. cancelled) -> serve_sales_orders",
    }


@pytest.fixture
def corrupted_target(duckdb_target):
    """Yields a function that applies the given SQL to the session's
    warehouse inside a transaction and returns the (now corrupted) target
    -- a simulated data-load failure. Rolled back afterwards, so the
    shared healthy target is untouched for every other test. (A file copy
    isn't an option: Windows locks the open DuckDB file, and the loaded
    vbak/vbap carry FK constraints that COPY FROM DATABASE trips over.)"""

    def _corrupt(*statements: str):
        duckdb_target._conn.execute("begin transaction")
        for sql in statements:
            duckdb_target._conn.execute(sql)
        return duckdb_target

    yield _corrupt
    duckdb_target._conn.execute("rollback")


def _flagged_tables(results):
    return {(r.table, r.metric) for r in results if r.status == "flag"}


def test_filtered_checks_still_catch_a_partial_load(postgres_source, corrupted_target):
    """~10% of non-cancelled order items never arrived in the warehouse.
    landing/prep are views over the loaded vbap, so both must flag."""
    target = corrupted_target("delete from vbap where order_id % 10 = 0")
    flagged = _flagged_tables(
        run_aggregate_checks(postgres_source, target, "dev")
    )
    assert ("vbap -> landing_vbap", "row_count") in flagged
    assert ("vbap -> landing_vbap", "sum_net_value") in flagged
    assert ("vbap (excl. cancelled) -> prep_sales_orders", "row_count") in flagged


def test_filtered_checks_still_catch_duplicated_rows(postgres_source, corrupted_target):
    # vbap has a (order_id, item_id) primary key, so the "duplicates" are
    # re-inserted under shifted item_ids -- the same rows loaded twice.
    target = corrupted_target(
        "insert into vbap select order_id, item_id + 100000, material_id, quantity, net_value "
        "from vbap where order_id % 5 = 0"
    )
    flagged = _flagged_tables(
        run_aggregate_checks(postgres_source, target, "dev")
    )
    assert ("vbap -> landing_vbap", "row_count") in flagged
    assert ("vbap (excl. cancelled) -> prep_sales_orders", "row_count") in flagged


def test_filtered_checks_still_catch_serve_layer_row_loss(postgres_source, corrupted_target):
    """serve_sales_orders is a materialized table: rows lost there alone
    (landing and prep intact) must flag serve and only serve."""
    target = corrupted_target("delete from serve_sales_orders where sales_order_id % 4 = 0")
    results = run_aggregate_checks(postgres_source, target, "dev")
    flagged = _flagged_tables(results)
    assert flagged == {
        ("vbap (excl. cancelled) -> serve_sales_orders", "row_count"),
        ("vbap (excl. cancelled) -> serve_sales_orders", "sum_net_value"),
    }
