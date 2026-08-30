"""classify_discrepancy -- the only node in this codebase allowed to call an
LLM (see CLAUDE.md). Forces structured (JSON-schema) output via Anthropic
tool-use, never free text a downstream step has to parse.

The LLM only ever produces (classification, confidence, reasoning). The
decision logic that turns that into a *final* classification -- downgrading
an under-confident "expected", always routing "needs_review" to a human and
"anomaly" to a ticket regardless of confidence -- is deterministic Python in
this file, not left to the model.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import anthropic
import yaml

from agent.state import AgentState, Classification, ClassificationResult

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "environments.yml"
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

_CLASSIFY_TOOL = {
    "name": "classify_discrepancy",
    "description": (
        "Classify a data-observability reconciliation discrepancy between a "
        "source ERP table and a dbt-transformed target table."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": ["expected", "needs_review", "anomaly"],
                "description": (
                    "'expected' if the SQL diff and PR description genuinely "
                    "explain the direction and magnitude of the discrepancy. "
                    "'needs_review' if there's a plausible explanation but it "
                    "isn't fully corroborated by the diff, or evidence is "
                    "mixed/incomplete. 'anomaly' if nothing in the diff or PR "
                    "explains it, especially if the affected table/metric is "
                    "untouched by the diff entirely."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Confidence in the chosen classification, grounded in "
                    "whether the SQL diff itself corroborates the direction "
                    "and magnitude of the discrepancy -- not how plausible "
                    "the PR description sounds on its own. A PR description "
                    "that asserts 'no behavior change' or an unrelated change, "
                    "with a diff that does not actually explain the "
                    "discrepancy, should lower confidence even if you lean "
                    "toward 'expected'."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Short explanation of the evidence used, in 2-4 sentences.",
            },
        },
        "required": ["classification", "confidence", "reasoning"],
    },
}


def _load_confidence_threshold(environment: str) -> float:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    return config["environments"][environment]["auto_actions"]["confidence_threshold"]


def _format_results(state: AgentState) -> str:
    lines = []
    for result in state.reconciliation_run.results:
        marker = "FLAGGED" if result.status == "flag" else "pass"
        lines.append(
            f"- [{marker}] check_type={result.check_type} table={result.table} "
            f"metric={result.metric} source_value={result.source_value} "
            f"target_value={result.target_value} diff_pct={result.diff_pct:.2f}% "
            f"threshold={result.threshold:.2f}%"
        )
    return "\n".join(lines) if lines else "(no results)"


def _build_prompt(state: AgentState) -> str:
    touched = ", ".join(state.diff_touched_tables) if state.diff_touched_tables else "(none)"
    return f"""A dbt code change was deployed and reconciliation was run against the source ERP data.
Reconciliation results (environment={state.reconciliation_run.environment}):
{_format_results(state)}

Tables/metrics involved in the above results that the SQL diff actually mentions: {touched}

--- SQL diff ---
{state.sql_diff or "(no diff provided)"}

--- PR description ---
{state.pr_description or "(no description provided)"}

Classify the FLAGGED result(s) above. Your confidence must reflect whether the SQL diff
itself corroborates the direction and magnitude of the discrepancy -- a plausible-sounding
PR description alone is not sufficient evidence."""


def _apply_decision_logic(raw: ClassificationResult, confidence_threshold: float) -> tuple[Classification, bool]:
    """Deterministic post-processing of the LLM's raw classification. Never
    left to the model -- see module docstring."""
    if raw.classification == "expected":
        if raw.confidence >= confidence_threshold:
            return "expected", False
        return "needs_review", True  # don't trust an under-confident "expected" blindly
    if raw.classification == "needs_review":
        return "needs_review", False  # always human review, regardless of confidence
    return "anomaly", False  # always ticket, regardless of confidence


def classify_discrepancy(state: AgentState) -> dict:
    confidence_threshold = _load_confidence_threshold(state.reconciliation_run.environment)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=1024,
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_discrepancy"},
        messages=[{"role": "user", "content": _build_prompt(state)}],
    )

    tool_use_block = next(block for block in response.content if block.type == "tool_use")
    raw = ClassificationResult(**tool_use_block.input)

    final_classification, downgraded = _apply_decision_logic(raw, confidence_threshold)

    logger.info(
        "classify_discrepancy: llm=%s (confidence=%.2f, threshold=%.2f) -> final=%s%s",
        raw.classification,
        raw.confidence,
        confidence_threshold,
        final_classification,
        " (downgraded)" if downgraded else "",
    )

    return {
        "classification": raw,
        "final_classification": final_classification,
        "downgraded": downgraded,
    }
