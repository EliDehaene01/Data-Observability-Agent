"""Tests for connectors/source/postgres_source.py and
connectors/target/duckdb_target.py against real local databases (see
conftest.py) -- no mocking. Covers:
  - get_row_count, get_aggregate, sample_rows, get_schema return
    consistent shapes across both engines
  - the SQL-injection-safe agg_func/operator whitelists in base.py reject
    invalid values
"""

from __future__ import annotations

import pytest

# -- Consistent shapes across engines ---------------------------------------
# landing_vbak/landing_vbap are dbt's 1:1, no-filtering copies of vbak/vbap
# (see dbt_project/models/landing/), so these should match the source
# exactly -- not just "same type", but the same values.


def test_get_row_count_consistent_shape_and_value(postgres_source, duckdb_target):
    source_count = postgres_source.get_row_count("vbak")
    target_count = duckdb_target.get_row_count("landing_vbak")

    assert type(source_count) is int
    assert type(target_count) is int
    assert source_count == target_count
    assert source_count > 0


def test_get_row_count_with_filter_consistent(postgres_source, duckdb_target):
    source_count = postgres_source.get_row_count("vbak", filters={"status": "cancelled"})
    target_count = duckdb_target.get_row_count("landing_vbak", filters={"status": "cancelled"})

    assert type(source_count) is int
    assert type(target_count) is int
    assert source_count == target_count
    assert source_count > 0


def test_get_aggregate_consistent_shape_and_value(postgres_source, duckdb_target):
    source_sum = postgres_source.get_aggregate("vbap", "net_value", "sum")
    target_sum = duckdb_target.get_aggregate("landing_vbap", "net_value", "sum")

    assert type(source_sum) is float
    assert type(target_sum) is float
    assert source_sum == pytest.approx(target_sum)
    assert source_sum > 0


def test_sample_rows_consistent_shape(postgres_source, duckdb_target):
    source_rows = postgres_source.sample_rows("vbak", 5)
    target_rows = duckdb_target.sample_rows("landing_vbak", 5)

    assert isinstance(source_rows, list) and len(source_rows) == 5
    assert isinstance(target_rows, list) and len(target_rows) == 5
    assert all(isinstance(row, dict) for row in source_rows + target_rows)
    assert set(source_rows[0].keys()) == set(target_rows[0].keys())
    assert set(source_rows[0].keys()) == {"order_id", "customer_id", "order_date", "status"}


def test_get_schema_consistent_shape(postgres_source, duckdb_target):
    source_schema = postgres_source.get_schema("vbak")
    target_schema = duckdb_target.get_schema("landing_vbak")

    assert [c.name for c in source_schema] == [c.name for c in target_schema]
    assert [c.name for c in source_schema] == ["order_id", "customer_id", "order_date", "status"]
    for column in source_schema + target_schema:
        assert isinstance(column.name, str)
        assert isinstance(column.data_type, str)


# -- Injection-safe whitelists -----------------------------------------------


def test_source_rejects_invalid_agg_func(postgres_source):
    with pytest.raises(ValueError):
        postgres_source.get_aggregate("vbap", "net_value", "sum(1); drop table vbap; --")


def test_target_rejects_invalid_agg_func(duckdb_target):
    with pytest.raises(ValueError):
        duckdb_target.get_aggregate("landing_vbap", "net_value", "; delete from landing_vbap; --")


def test_source_rejects_invalid_filter_operator(postgres_source):
    with pytest.raises(ValueError):
        postgres_source.get_row_count("vbak", filters={"status": ("; drop table vbak; --", "x")})


def test_target_rejects_invalid_filter_operator(duckdb_target):
    with pytest.raises(ValueError):
        duckdb_target.get_row_count("landing_vbak", filters={"status": ("OR 1=1 --", "x")})


def test_valid_agg_funcs_and_operators_still_work(postgres_source):
    """The whitelist rejects garbage but doesn't accidentally reject
    everything real callers use."""
    assert postgres_source.get_aggregate("vbap", "quantity", "avg") > 0
    assert postgres_source.get_row_count("vbak", filters={"order_id": (">=", 0)}) > 0
