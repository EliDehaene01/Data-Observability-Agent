"""Shared state schema for the LangGraph reconciliation-reasoning agent.
Every node reads and writes this single schema -- no node invents its own
ad hoc state shape (see CLAUDE.md).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from reconciliation.models import ReconciliationRun

Classification = Literal["expected", "needs_review", "anomaly"]


class ClassificationResult(BaseModel):
    """Raw output of the classify_discrepancy LLM call, before the
    deterministic decision logic is applied."""

    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    pr_claims_no_impact: bool
    """True if the PR description asserts/implies no behavior change or no
    impact, independent of whether the diff actually corroborates that
    claim. Scored separately from `confidence` because a diagnostic found
    the model can score high confidence in 'expected' from a diff that
    mechanically explains a metric's direction/magnitude, even while its
    own reasoning says the PR's impact claim is false -- confidence alone
    doesn't capture PR dishonesty. See _apply_decision_logic's PR-honesty
    override in classify_discrepancy.py."""


class AgentState(BaseModel):
    # -- Inputs --
    run_id: Optional[str] = None
    """Which ReconciliationRun to evaluate. Only needed if `reconciliation_run`
    isn't already provided directly (e.g. by a test) -- fetch_reconciliation_results
    fetches it from results_store when this is the only thing set."""
    sql_diff: str = ""
    pr_description: str = ""

    # -- Populated by fetch_reconciliation_results --
    reconciliation_run: Optional[ReconciliationRun] = None

    # -- Populated by analyze_diff --
    diff_touched_tables: list[str] = Field(default_factory=list)
    """Table names that literally appear in sql_diff, out of the tables
    involved in reconciliation_run's results. Deterministic, not LLM-derived."""

    # -- Populated by classify_discrepancy --
    classification: Optional[ClassificationResult] = None
    """The LLM's raw output, before the decision logic below."""
    final_classification: Optional[Classification] = None
    """classification after the decision logic (e.g. downgrading a
    low-confidence "expected" to "needs_review"). The graph branches on
    this, never on `classification` directly."""
    downgraded: bool = False
    """True if final_classification differs from classification.classification --
    either the confidence-threshold downgrade (expected -> needs_review) or
    the PR-honesty override (any raw classification -> needs_review)."""

    # -- Populated by the drafting/action nodes (stubbed until phase 9) --
    confluence_doc: Optional[str] = None
    jira_ticket: Optional[str] = None
    slack_message: Optional[str] = None
