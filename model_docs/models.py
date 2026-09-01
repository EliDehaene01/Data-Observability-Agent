"""Structured (pydantic) payloads for the per-model documentation pipeline.

`ModelStructure` / `ModelColumn` / `ServeField` are filled *deterministically*
from dbt's manifest.json + catalog.json (see model_docs/manifest.py).
`ModelLogicSummary` is the forced-structured output of the one LLM node in
this pipeline (agent/nodes/summarize_model_logic.py) -- kept in its own
model so nothing conflates the deterministic structure with the generated
prose.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ModelColumn(BaseModel):
    """One output column of a dbt model, from catalog.json (authoritative,
    warehouse-observed) or, if no catalog was generated, from the columns
    documented in schema.yml via manifest.json."""

    name: str
    data_type: str
    source: str = "catalog"
    """"catalog" (warehouse-observed) or "manifest" (schema.yml only) -- so a
    reader can tell whether the column list is complete."""


class ModelStructure(BaseModel):
    """Everything about a dbt model that comes from dbt's own artifacts and
    never from an LLM: its layer, its ref()/source() lineage, and its
    output columns."""

    name: str
    layer: str
    """landing / prep / serve -- derived from the model's path."""
    relative_path: str
    schema_name: str
    source_tables: list[str] = Field(default_factory=list)
    """Fully-qualified source() tables the model reads, e.g. "mock_erp.vbak"."""
    referenced_models: list[str] = Field(default_factory=list)
    """Other dbt models this one ref()s, by name."""
    columns: list[ModelColumn] = Field(default_factory=list)
    compiled_sql: str = ""
    columns_from_catalog: bool = True
    """False when catalog.json was unavailable and `columns` is only the
    schema.yml-documented subset."""


class ModelLogicSummary(BaseModel):
    """Forced-structured output of agent/nodes/summarize_model_logic.py -- the
    second, deliberate LLM use in this codebase (the first being
    classify_discrepancy). Plain-English only; no structural claims, those
    come from ModelStructure."""

    summary: str
    """2-5 sentences: what business logic / transformation this model performs."""
    key_transformations: list[str] = Field(default_factory=list)
    """Short bullet phrases, e.g. "excludes cancelled orders"."""


class ServeField(BaseModel):
    """One row of the Serve Layer Overview data dictionary. The
    business-friendly name is just the serve column name itself -- the serve
    layer is rename-only (see CLAUDE.md), so its column names *are* the
    business-friendly names, no LLM needed."""

    model: str
    field: str
    data_type: str
    description: Optional[str] = None
    """From schema.yml (manifest.json) when documented, else None."""
