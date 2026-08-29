"""Structured (pydantic) output for the deterministic reconciliation engine.

Everything in reconciliation/ produces these models and nothing else --
agent/ consumes them without ever re-deriving a result itself. No LLM code
belongs in this file (see CLAUDE.md).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CheckType = Literal["aggregate", "sample"]
CheckStatus = Literal["pass", "flag"]
TriggerType = Literal["data_load", "code_change"]


class ReconciliationResult(BaseModel):
    """One source-vs-target comparison (one metric, one table pair)."""

    check_type: CheckType
    table: str
    metric: str
    source_value: float
    target_value: float
    diff_pct: float
    threshold: float
    status: CheckStatus
    environment: str
    run_timestamp: datetime


class ReconciliationRun(BaseModel):
    """All the ReconciliationResults produced by a single run, plus the
    metadata needed to know which trigger and environment produced them."""

    environment: str
    run_timestamp: datetime
    trigger_type: TriggerType
    results: list[ReconciliationResult] = Field(default_factory=list)

    @property
    def has_flags(self) -> bool:
        return any(result.status == "flag" for result in self.results)
