"""Tests for reconciliation/aggregate_checks.py and sample_checks.py --
deterministic engine only, no LLM, no mocking of the connectors (real
Postgres/DuckDB, see conftest.py). Covers:
  - thresholds are actually read from config/environments.yml per environment
  - status flips pass -> flag exactly at the threshold boundary
  - the known cancelled-order divergence produces the expected diff_pct
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


# -- Known cancelled-order divergence ---------------------------------------


def test_cancelled_order_divergence_matches_expected_magnitude(postgres_source, duckdb_target):
    """prep_sales_orders excludes cancelled orders entirely (see
    dbt_project/models/prep/prep_sales_orders.sql) -- the resulting
    row_count divergence should track the actual cancelled-order rate in
    the source data, computed independently here rather than hardcoded, so
    this doesn't silently rot if the seed data is regenerated with
    different volumes."""
    total_orders = postgres_source.get_row_count("vbak")
    cancelled_orders = postgres_source.get_row_count("vbak", filters={"status": "cancelled"})
    assert cancelled_orders > 0, "seed data should include some cancelled orders"
    expected_order_level_exclusion_pct = cancelled_orders / total_orders * 100

    results = run_aggregate_checks(postgres_source, duckdb_target, "dev")
    row_count_result = next(
        r for r in results if r.table == "vbap -> prep_sales_orders" and r.metric == "row_count"
    )

    assert row_count_result.source_value > row_count_result.target_value
    assert row_count_result.diff_pct > 0
    # item-level exclusion rate tracks the order-level rate (not exact,
    # since line-items-per-order varies by status, but should be close).
    assert row_count_result.diff_pct == pytest.approx(expected_order_level_exclusion_pct, abs=5.0)
    # this divergence is large relative to every environment's threshold
    assert row_count_result.status == "flag"


def test_serve_sales_orders_matches_prep_sales_orders_divergence(postgres_source, duckdb_target):
    """serve_sales_orders is a faithful rename of prep_sales_orders (see
    dbt_project/models/serve/serve_sales_orders.sql) -- its divergence from
    source should be identical."""
    results = run_aggregate_checks(postgres_source, duckdb_target, "dev")
    prep_result = next(
        r for r in results if r.table == "vbap -> prep_sales_orders" and r.metric == "row_count"
    )
    serve_result = next(
        r for r in results if r.table == "vbap -> serve_sales_orders" and r.metric == "row_count"
    )
    assert prep_result.diff_pct == serve_result.diff_pct
