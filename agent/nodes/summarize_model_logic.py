"""summarize_model_logic -- the SECOND deliberate LLM use in this codebase,
separate from classify_discrepancy (see CLAUDE.md's "hard architectural
rules").

Scope, and why it's a separate node:
  - classify_discrepancy reasons about whether a reconciliation *discrepancy*
    is expected/needs_review/anomaly. It runs inside agent/graph.py, on the
    code-change *validation* path, and its output gates PR merges.
  - summarize_model_logic does one narrow thing: turn a dbt model's compiled
    SQL into a plain-English description of the business logic it performs,
    for the per-model Confluence documentation pages. It is NOT in
    agent/graph.py, it never sees reconciliation results, and its output
    gates nothing.

Deterministic structural facts about a model (its source tables, ref()
lineage, column names/types) are NEVER produced here -- those come straight
from dbt's manifest.json / catalog.json in model_docs/manifest.py. This node
only ever describes *logic*.

Forces structured (JSON-schema) output via Anthropic tool-use, exactly like
classify_discrepancy -- never free text a downstream step has to parse.
"""

from __future__ import annotations

import logging
import os
import re

import anthropic

from model_docs.models import ModelLogicSummary

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

_SUMMARIZE_TOOL = {
    "name": "summarize_model_logic",
    "description": (
        "Summarize, in plain business English, the transformation logic a dbt "
        "model performs -- based only on its compiled SQL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-5 sentences describing what business logic / transformation "
                    "this model performs: what it selects, joins, filters, "
                    "aggregates, renames, or flags, and why that matters to a "
                    "business reader. Describe the logic, not the syntax. Do not "
                    "list column names or source tables exhaustively -- those are "
                    "documented separately from dbt's own metadata."
                ),
            },
            "key_transformations": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Short phrases naming each distinct transformation, e.g. "
                    "'excludes cancelled orders', 'flags incomplete orders', "
                    "'aggregates to customer-month grain'. Empty list if the model "
                    "is a straight pass-through with no transformation."
                ),
            },
        },
        "required": ["summary", "key_transformations"],
    },
}

_TAG_LIKE = re.compile(r"</?[A-Za-z_][\w:.\-]*(?:\s[^<>]*)?/?>")


def _sanitize(text: str) -> str:
    """Same defensive strip as classify_discrepancy: the model occasionally
    leaks stray tool-call-format tokens into free-text fields, and this text
    goes straight into a human-facing Confluence page."""
    cleaned = _TAG_LIKE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _build_prompt(model_name: str, layer: str, compiled_sql: str) -> str:
    return f"""A dbt model named `{model_name}` (in the `{layer}` layer) compiles to the SQL below.
Summarize the business logic it performs, for a data-dictionary page aimed at business
and analytics-engineering readers.

--- compiled SQL ---
{compiled_sql or "(no compiled SQL available)"}
"""


def summarize_model_logic(
    model_name: str,
    compiled_sql: str,
    layer: str = "",
) -> ModelLogicSummary:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=1024,
        tools=[_SUMMARIZE_TOOL],
        tool_choice={"type": "tool", "name": "summarize_model_logic"},
        messages=[{"role": "user", "content": _build_prompt(model_name, layer, compiled_sql)}],
    )

    tool_use_block = next(block for block in response.content if block.type == "tool_use")
    tool_input = dict(tool_use_block.input)
    tool_input["summary"] = _sanitize(tool_input.get("summary", ""))
    tool_input["key_transformations"] = [
        _sanitize(item) for item in tool_input.get("key_transformations", []) if _sanitize(item)
    ]
    summary = ModelLogicSummary(**tool_input)
    logger.info(
        "summarize_model_logic: %s -> %d chars, %d transformations",
        model_name,
        len(summary.summary),
        len(summary.key_transformations),
    )
    return summary
