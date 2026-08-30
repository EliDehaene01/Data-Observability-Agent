"""classify_discrepancy -- the only node in this codebase allowed to call an
LLM (see CLAUDE.md). Forces structured (JSON-schema) output via Anthropic
tool-use, never free text a downstream step has to parse.

The LLM only ever produces (classification, confidence, reasoning,
pr_claims_no_impact). The decision logic that turns that into a *final*
classification is deterministic Python in this file, not left to the
model -- see _apply_decision_logic's docstring for the two rules (a
confidence-threshold downgrade, and a PR-honesty override that fires
regardless of confidence).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import anthropic
import yaml

from agent.state import AgentState, Classification, ClassificationResult
from reconciliation.models import ReconciliationRun

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
            "pr_claims_no_impact": {
                "type": "boolean",
                "description": (
                    "True if the PR description asserts or implies there is no "
                    "behavior change, no impact, or that the change is 'safe' -- "
                    "regardless of whether the diff actually corroborates that "
                    "claim. Score this independently of `confidence`: "
                    "`confidence` measures whether the diff explains the "
                    "reconciliation metric's direction/magnitude; this field "
                    "measures whether the PR's own narrative about impact is "
                    "honest. A PR can claim no impact while the diff clearly "
                    "shows a change that would have impact -- that combination "
                    "is exactly what this field exists to flag, even if you "
                    "still lean toward 'expected' with high confidence."
                ),
            },
        },
        "required": ["classification", "confidence", "reasoning", "pr_claims_no_impact"],
    },
}


_TAG_LIKE = re.compile(r"</?[A-Za-z_][\w:.\-]*(?:\s[^<>]*)?/?>")


def _sanitize_reasoning(text: str) -> str:
    """The model occasionally leaks stray tool-call-format tokens (e.g.
    `</invoke>`, `</reasoning>`) into free-text fields. This strips any
    XML/HTML-like tag and tidies up the resulting whitespace, since
    reasoning now goes straight into human-facing Confluence pages and
    Slack messages, not just logs."""
    cleaned = _TAG_LIKE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


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
PR description alone is not sufficient evidence. Separately, set pr_claims_no_impact based
on what the PR description asserts about impact, regardless of whether that assertion holds up."""


def _has_uncorroborated_flagged_result(reconciliation_run: ReconciliationRun) -> bool:
    """True if any result actually exceeded its threshold -- i.e. the kind
    of discrepancy classify_discrepancy is being asked to explain in the
    first place. Compared directly (diff_pct vs threshold) rather than
    trusting the upstream `status` field, so this check doesn't silently
    depend on how reconciliation/ happens to set it."""
    return any(result.diff_pct > result.threshold for result in reconciliation_run.results)


def _apply_decision_logic(
    raw: ClassificationResult,
    confidence_threshold: float,
    reconciliation_run: ReconciliationRun,
    diff_touched_tables: list[str],
) -> tuple[Classification, bool]:
    """Deterministic post-processing of the LLM's raw classification. Never
    left to the model -- see module docstring. Two rules, applied in order:

    1. Confidence gating: an "expected" classification is only trusted at
       or above the environment's confidence_threshold; below it, downgrade
       to "needs_review". "needs_review"/"anomaly" are never gated on
       confidence.
    2. PR-honesty override: regardless of rule 1's outcome (and regardless
       of the LLM's raw classification or confidence), if the PR claims no
       impact while a reconciliation result actually exceeded its
       threshold, force "needs_review". A live diagnostic found the model
       can score confidence comfortably above threshold for an "expected"
       classification even while its own reasoning says the PR's no-impact
       claim is contradicted -- confidence alone doesn't capture PR
       dishonesty, so this is checked separately and unconditionally, the
       same way "anomaly" is never gated on confidence.

       This rule additionally requires diff_touched_tables to be non-empty:
       a "no impact" claim is only a dangerous claim *about the flagged
       discrepancy* if the diff actually touches the affected table(s). A
       genuinely no-op change (e.g. a comment) on a table that has nothing
       to do with the divergence correctly earns pr_claims_no_impact=True
       too, but that's irrelevant information, not PR dishonesty -- the
       untouched-table case is already handled correctly by "anomaly" via
       diff_touched_tables being empty, and this rule must not override
       that (confirmed by a regression check after first shipping this
       rule without the guard).
    """
    if raw.classification == "expected":
        if raw.confidence >= confidence_threshold:
            classification, downgraded = "expected", False
        else:
            classification, downgraded = "needs_review", True  # don't trust an under-confident "expected" blindly
    elif raw.classification == "needs_review":
        classification, downgraded = "needs_review", False  # always human review, regardless of confidence
    else:
        classification, downgraded = "anomaly", False  # always ticket, regardless of confidence

    if (
        raw.pr_claims_no_impact
        and diff_touched_tables
        and _has_uncorroborated_flagged_result(reconciliation_run)
    ):
        if classification != "needs_review":
            downgraded = True
        classification = "needs_review"

    return classification, downgraded


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
    tool_input = dict(tool_use_block.input)
    tool_input["reasoning"] = _sanitize_reasoning(tool_input["reasoning"])
    raw = ClassificationResult(**tool_input)

    final_classification, downgraded = _apply_decision_logic(
        raw, confidence_threshold, state.reconciliation_run, state.diff_touched_tables
    )

    logger.info(
        "classify_discrepancy: llm=%s (confidence=%.2f, threshold=%.2f, pr_claims_no_impact=%s) -> final=%s%s",
        raw.classification,
        raw.confidence,
        confidence_threshold,
        raw.pr_claims_no_impact,
        final_classification,
        " (downgraded)" if downgraded else "",
    )

    return {
        "classification": raw,
        "final_classification": final_classification,
        "downgraded": downgraded,
    }
