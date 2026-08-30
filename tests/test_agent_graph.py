"""Tests for agent/nodes/classify_discrepancy.py's decision logic --
confidence gating and the PR-honesty override that together decide the
*final* classification agent/graph.py branches on.

classify_discrepancy() calls the real Anthropic API. Every test in this
file mocks that boundary (patches anthropic.Anthropic so no network call
happens and no API key is required) and exercises the real function,
including tool-use response parsing -- not just _apply_decision_logic in
isolation. See tests/README.md for the small number of @pytest.mark.live
tests that call the real model instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.nodes.classify_discrepancy import classify_discrepancy
from agent.state import AgentState
from reconciliation.models import ReconciliationResult, ReconciliationRun

# dev's confidence_threshold (config/environments.yml) -- used throughout
# to pick confidence values clearly above/below the gate.
DEV_THRESHOLD = 0.6


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    # classify_discrepancy reads this before anthropic.Anthropic is even
    # constructed, so it must be set even though the client itself is mocked.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")


def _make_state(
    diff_touched_tables=(),
    environment="dev",
    diff_pct=20.0,
    threshold=5.0,
    table="vbap -> prep_sales_orders",
    sql_diff="-- some diff",
    pr_description="some description",
):
    now = datetime.now(timezone.utc)
    run = ReconciliationRun(
        environment=environment,
        run_timestamp=now,
        trigger_type="code_change",
        results=[
            ReconciliationResult(
                check_type="aggregate",
                table=table,
                metric="row_count",
                source_value=100.0,
                target_value=100.0 * (1 - diff_pct / 100),
                diff_pct=diff_pct,
                threshold=threshold,
                status="flag" if diff_pct > threshold else "pass",
                environment=environment,
                run_timestamp=now,
            )
        ],
    )
    return AgentState(
        reconciliation_run=run,
        sql_diff=sql_diff,
        pr_description=pr_description,
        diff_touched_tables=list(diff_touched_tables),
    )


def _mock_llm(state, classification, confidence, pr_claims_no_impact, reasoning="mock reasoning"):
    tool_use_block = SimpleNamespace(
        type="tool_use",
        input={
            "classification": classification,
            "confidence": confidence,
            "reasoning": reasoning,
            "pr_claims_no_impact": pr_claims_no_impact,
        },
    )
    fake_response = SimpleNamespace(content=[tool_use_block])
    with patch("agent.nodes.classify_discrepancy.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = fake_response
        return classify_discrepancy(state)


# -- Confidence gating -------------------------------------------------------


def test_expected_at_or_above_threshold_stays_expected():
    state = _make_state(diff_touched_tables=["prep_sales_orders"])
    result = _mock_llm(state, "expected", DEV_THRESHOLD, pr_claims_no_impact=False)
    assert result["final_classification"] == "expected"
    assert result["downgraded"] is False


def test_expected_below_threshold_downgrades_to_needs_review():
    state = _make_state(diff_touched_tables=["prep_sales_orders"])
    result = _mock_llm(state, "expected", DEV_THRESHOLD - 0.01, pr_claims_no_impact=False)
    assert result["final_classification"] == "needs_review"
    assert result["downgraded"] is True


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.99])
def test_needs_review_always_needs_review_regardless_of_confidence(confidence):
    state = _make_state(diff_touched_tables=["prep_sales_orders"])
    result = _mock_llm(state, "needs_review", confidence, pr_claims_no_impact=False)
    assert result["final_classification"] == "needs_review"
    assert result["downgraded"] is False


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.99])
def test_anomaly_always_anomaly_never_downgraded(confidence):
    state = _make_state(diff_touched_tables=["prep_sales_orders"])
    result = _mock_llm(state, "anomaly", confidence, pr_claims_no_impact=False)
    assert result["final_classification"] == "anomaly"
    assert result["downgraded"] is False


# -- PR-honesty override ------------------------------------------------------


def test_override_forces_needs_review_from_confident_expected():
    """The failure mode the diagnostic session found: a diff that
    mechanically explains a metric's direction/magnitude can earn high
    confidence even while pr_claims_no_impact is true and the claim is
    false. The override must catch this regardless of confidence."""
    state = _make_state(diff_touched_tables=["prep_sales_orders"])
    result = _mock_llm(state, "expected", 0.95, pr_claims_no_impact=True)
    assert result["final_classification"] == "needs_review"
    assert result["downgraded"] is True


def test_override_forces_needs_review_even_from_anomaly():
    """The override is unconditional on raw classification, same as anomaly
    is never gated on confidence -- it can downgrade a raw "anomaly" too."""
    state = _make_state(diff_touched_tables=["prep_sales_orders"])
    result = _mock_llm(state, "anomaly", 0.95, pr_claims_no_impact=True)
    assert result["final_classification"] == "needs_review"
    assert result["downgraded"] is True


def test_override_does_not_fire_when_diff_touched_tables_is_empty():
    """The edge case caught by a regression check in the earlier diagnostic
    session: a genuinely no-op change (e.g. a comment) on a table unrelated
    to the divergence correctly earns pr_claims_no_impact=True too, but
    that's irrelevant information, not PR dishonesty about THIS
    discrepancy. Must not override the (correct) "anomaly" classification
    for a divergence in a table the diff never touched."""
    state = _make_state(diff_touched_tables=[])
    result = _mock_llm(state, "anomaly", 0.95, pr_claims_no_impact=True)
    assert result["final_classification"] == "anomaly"
    assert result["downgraded"] is False


def test_override_requires_an_actually_flagged_result():
    """pr_claims_no_impact alone isn't enough -- there must be a real
    divergence (diff_pct > threshold) for the override to mean anything."""
    state = _make_state(diff_touched_tables=["prep_sales_orders"], diff_pct=1.0, threshold=5.0)
    result = _mock_llm(state, "expected", 0.95, pr_claims_no_impact=True)
    assert result["final_classification"] == "expected"
    assert result["downgraded"] is False


def test_override_does_not_fire_when_pr_is_honest():
    """pr_claims_no_impact=False -- even with a real divergence and a diff
    that touches the flagged table -- must never trigger the override."""
    state = _make_state(diff_touched_tables=["prep_sales_orders"])
    result = _mock_llm(state, "expected", 0.95, pr_claims_no_impact=False)
    assert result["final_classification"] == "expected"
    assert result["downgraded"] is False


# -- Live sanity checks (real Anthropic API; opt-in via --run-live) ---------
#
# These reuse the two scenarios that were reliably consistent across many
# live runs during earlier manual verification sessions. They exist to
# manually sanity-check against the real model after a prompt/schema
# change -- not for routine CI, since live LLM output is inherently
# non-deterministic (see the diagnostic session that found the PR-honesty
# override gap in the first place).


@pytest.mark.live
def test_live_diff_clearly_explains_discrepancy_is_expected():
    state = _make_state(
        diff_touched_tables=["prep_sales_orders"],
        diff_pct=17.26,
        threshold=5.0,
        sql_diff="""
--- a/dbt_project/models/prep/prep_sales_orders.sql
+++ b/dbt_project/models/prep/prep_sales_orders.sql
@@ -5,6 +5,8 @@ with headers as (
     select *
     from {{ ref('landing_vbak') }}
+    -- Business rule: cancelled orders are excluded entirely.
+    where status != 'cancelled'

 ),
""",
        pr_description=(
            "Adds a business rule to exclude cancelled orders from prep_sales_orders, "
            "per finance's request. Cancelled orders are roughly 17% of order volume in "
            "the current seed data, matching the expected drop in row_count downstream."
        ),
    )
    result = classify_discrepancy(state)
    assert result["final_classification"] == "expected"


@pytest.mark.live
def test_live_divergence_in_untouched_table_is_anomaly():
    state = _make_state(
        diff_touched_tables=[],
        diff_pct=12.0,
        threshold=5.0,
        table="vbap -> serve_monthly_revenue",
        sql_diff="""
--- a/dbt_project/models/landing/landing_vbak.sql
+++ b/dbt_project/models/landing/landing_vbak.sql
@@ -1,4 +1,5 @@
+-- clarify VBELN meaning
 select
     cast(order_id as integer)   as order_id,
""",
        pr_description="Add a clarifying comment to landing_vbak.sql explaining the order_id column.",
    )
    result = classify_discrepancy(state)
    assert result["final_classification"] == "anomaly"
