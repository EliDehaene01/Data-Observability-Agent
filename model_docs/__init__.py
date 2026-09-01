"""Per-model dbt documentation pipeline (PR-triggered on dbt_project/models/**).

Deterministic structure -- source tables, ref()/source() lineage, and column
names/types -- comes straight from dbt's own artifacts (manifest.json +
catalog.json). No LLM infers any of that (see CLAUDE.md).

The one LLM-derived piece, the plain-English "what business logic does this
model perform" summary, is produced by a *separate* node --
agent/nodes/summarize_model_logic.py -- not by anything in this package.
This package only parses artifacts and renders Confluence storage-format
pages; scripts/generate_model_docs.py wires the two together.
"""
