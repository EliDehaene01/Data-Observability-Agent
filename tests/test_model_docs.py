"""Tests for the per-model documentation pipeline:

  - model_docs/manifest.py -- deterministic parsing of dbt's manifest.json /
    catalog.json and of a unified diff. No mocking; small synthetic
    artifacts built inline.
  - model_docs/render.py  -- Confluence storage-format rendering + escaping.
  - agent/nodes/summarize_model_logic.py -- the second LLM node. The
    Anthropic call is mocked (same boundary as tests/test_agent_graph.py);
    a @pytest.mark.live test exercises the real model.

Nothing here calls a real database or a real Confluence.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.nodes.summarize_model_logic import summarize_model_logic
from model_docs.manifest import (
    build_model_structures,
    changed_model_names_from_diff,
    serve_field_descriptions,
    serve_fields,
)
from model_docs.models import ModelLogicSummary, ModelStructure
from model_docs.render import (
    SERVE_OVERVIEW_TITLE,
    model_page_title,
    render_model_page,
    render_serve_overview_page,
)

# -- synthetic dbt artifacts ------------------------------------------------

_MANIFEST = {
    "nodes": {
        "model.p.landing_vbak": {
            "resource_type": "model",
            "name": "landing_vbak",
            "path": "landing/landing_vbak.sql",
            "schema": "main",
            "sources": [["mock_erp", "vbak"]],
            "refs": [],
            "depends_on": {"nodes": ["source.p.mock_erp.vbak"]},
            "columns": {"order_id": {"data_type": "integer"}},
            "compiled_code": "select 1 as order_id",
        },
        "model.p.prep_sales_orders": {
            "resource_type": "model",
            "name": "prep_sales_orders",
            "path": "prep/prep_sales_orders.sql",
            "schema": "main",
            "sources": [],
            "refs": [{"name": "landing_vbak"}, {"name": "landing_vbap"}],
            "depends_on": {"nodes": ["model.p.landing_vbak", "model.p.landing_vbap"]},
            "columns": {},
            "compiled_code": "select * from landing_vbak where status != 'cancelled'",
        },
        "model.p.serve_sales_orders": {
            "resource_type": "model",
            "name": "serve_sales_orders",
            "path": "serve/serve_sales_orders.sql",
            "schema": "main",
            "sources": [],
            "refs": [{"name": "prep_sales_orders"}],
            "depends_on": {"nodes": ["model.p.prep_sales_orders"]},
            "columns": {
                "sales_order_id": {"description": "Business key for the order line."},
            },
            "compiled_code": "select order_id as sales_order_id from prep_sales_orders",
        },
    }
}

_CATALOG = {
    "nodes": {
        "model.p.serve_sales_orders": {
            "columns": {
                "SALES_ORDER_ID": {"name": "sales_order_id", "type": "INTEGER", "index": 1},
                "ORDER_STATUS": {"name": "order_status", "type": "VARCHAR", "index": 2},
            }
        },
        "model.p.landing_vbak": {
            "columns": {
                "ORDER_ID": {"name": "order_id", "type": "INTEGER", "index": 1},
                "STATUS": {"name": "status", "type": "VARCHAR", "index": 2},
            }
        },
    }
}


@pytest.fixture
def artifacts(tmp_path):
    manifest = tmp_path / "manifest.json"
    catalog = tmp_path / "catalog.json"
    manifest.write_text(json.dumps(_MANIFEST), encoding="utf-8")
    catalog.write_text(json.dumps(_CATALOG), encoding="utf-8")
    return str(manifest), str(catalog)


# -- diff parsing ----------------------------------------------------------


def test_changed_model_names_from_diff_extracts_sql_models_only():
    diff = (
        "diff --git a/dbt_project/models/prep/prep_sales_orders.sql b/dbt_project/models/prep/prep_sales_orders.sql\n"
        "--- a/dbt_project/models/prep/prep_sales_orders.sql\n"
        "+++ b/dbt_project/models/prep/prep_sales_orders.sql\n"
        "@@ -1 +1,2 @@\n+-- a change\n"
        "diff --git a/dbt_project/models/prep/schema.yml b/dbt_project/models/prep/schema.yml\n"
        "+++ b/dbt_project/models/prep/schema.yml\n"
        "diff --git a/README.md b/README.md\n"
    )
    assert changed_model_names_from_diff(diff) == ["prep_sales_orders"]


def test_changed_model_names_from_diff_handles_windows_separators_and_dedupes():
    diff = (
        "+++ b/dbt_project\\models\\serve\\serve_sales_orders.sql\n"
        "--- a/dbt_project\\models\\serve\\serve_sales_orders.sql\n"
    )
    assert changed_model_names_from_diff(diff) == ["serve_sales_orders"]


def test_changed_model_names_from_diff_empty_when_no_models_touched():
    assert changed_model_names_from_diff("diff --git a/main.py b/main.py\n") == []


# -- manifest / catalog parsing ------------------------------------------


def test_build_model_structures_lineage_and_layers(artifacts):
    manifest, catalog = artifacts
    structures = build_model_structures(manifest, catalog)

    assert structures["landing_vbak"].layer == "landing"
    assert structures["landing_vbak"].source_tables == ["mock_erp.vbak"]
    assert structures["landing_vbak"].referenced_models == []

    prep = structures["prep_sales_orders"]
    assert prep.layer == "prep"
    assert prep.source_tables == []
    assert prep.referenced_models == ["landing_vbak", "landing_vbap"]
    assert "cancelled" in prep.compiled_sql


def test_build_model_structures_columns_prefer_catalog(artifacts):
    manifest, catalog = artifacts
    structures = build_model_structures(manifest, catalog)

    serve = structures["serve_sales_orders"]
    assert serve.columns_from_catalog is True
    assert [c.name for c in serve.columns] == ["sales_order_id", "order_status"]
    assert serve.columns[0].data_type == "INTEGER"


def test_build_model_structures_falls_back_to_manifest_columns_without_catalog(artifacts):
    manifest, _catalog = artifacts
    structures = build_model_structures(manifest, catalog_path=None)

    landing = structures["landing_vbak"]
    assert landing.columns_from_catalog is False
    assert [c.name for c in landing.columns] == ["order_id"]
    assert landing.columns[0].source == "manifest"


def test_serve_fields_only_serve_models_with_descriptions(artifacts):
    manifest, catalog = artifacts
    structures = build_model_structures(manifest, catalog)
    descriptions = serve_field_descriptions(manifest)
    rows = serve_fields(structures, descriptions)

    assert {r.model for r in rows} == {"serve_sales_orders"}
    by_field = {r.field: r for r in rows}
    assert by_field["sales_order_id"].description == "Business key for the order line."
    assert by_field["order_status"].description is None


# -- rendering -----------------------------------------------------------


def test_render_model_page_contains_structure_and_summary():
    structure = ModelStructure(
        name="prep_sales_orders",
        layer="prep",
        relative_path="prep/prep_sales_orders.sql",
        schema_name="main",
        source_tables=["mock_erp.vbak"],
        referenced_models=["landing_vbak"],
        columns=[],
        compiled_sql="select 1",
    )
    summary = ModelLogicSummary(
        summary="Excludes cancelled orders & <keeps> incomplete ones.",
        key_transformations=["excludes cancelled orders"],
    )
    html_body = render_model_page(structure, summary)

    assert "mock_erp.vbak" in html_body
    assert "landing_vbak" in html_body
    assert "excludes cancelled orders" in html_body
    # dynamic text is escaped
    assert "&amp;" in html_body and "&lt;keeps&gt;" in html_body
    assert "LLM-generated" in html_body


def test_render_serve_overview_is_a_table_with_every_field():
    rows = [
        SimpleNamespace(model="serve_sales_orders", field="sales_order_id", data_type="INTEGER", description=None),
        SimpleNamespace(model="serve_monthly_revenue", field="revenue_month", data_type="TIMESTAMP", description="Month"),
    ]
    body = render_serve_overview_page(rows)
    assert "<table>" in body
    assert "sales_order_id" in body and "revenue_month" in body
    assert "rename-only" in body


def test_model_page_title_and_serve_title_are_stable():
    assert model_page_title("prep_sales_orders") == "dbt model: prep_sales_orders"
    assert SERVE_OVERVIEW_TITLE == "Serve Layer Overview"


# -- the LLM node (mocked) ---------------------------------------------


@pytest.fixture(autouse=True)
def _fake_api_key(request, monkeypatch):
    # Live tests need the real key from the environment; only the mocked
    # tests get a placeholder (classify/summarize read the env var before the
    # mocked client is constructed).
    if "live" not in request.keywords:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")


def _mock_summary(compiled_sql, summary_text, transformations):
    block = SimpleNamespace(
        type="tool_use",
        input={"summary": summary_text, "key_transformations": transformations},
    )
    response = SimpleNamespace(content=[block])
    with patch("agent.nodes.summarize_model_logic.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = response
        return summarize_model_logic("prep_sales_orders", compiled_sql, "prep")


def test_summarize_model_logic_returns_structured_output():
    result = _mock_summary(
        "select * from x where status != 'cancelled'",
        "Filters out cancelled orders.",
        ["excludes cancelled orders"],
    )
    assert isinstance(result, ModelLogicSummary)
    assert result.summary == "Filters out cancelled orders."
    assert result.key_transformations == ["excludes cancelled orders"]


def test_summarize_model_logic_strips_leaked_tags():
    result = _mock_summary("select 1", "Does a thing.</invoke>", ["clean <x>value</x>"])
    assert "</invoke>" not in result.summary
    assert result.key_transformations == ["clean value"]


@pytest.mark.live
def test_live_summarize_prep_model_mentions_cancelled_orders():
    sql = (
        "select h.order_id, h.status = 'incomplete' as is_incomplete\n"
        "from landing_vbak h where h.status != 'cancelled'"
    )
    result = summarize_model_logic("prep_sales_orders", sql, "prep")
    joined = (result.summary + " " + " ".join(result.key_transformations)).lower()
    assert "cancel" in joined
