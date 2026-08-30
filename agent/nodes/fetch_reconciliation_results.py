"""fetch_reconciliation_results -- the graph's entry node. Deterministic,
no LLM call. Per CLAUDE.md, agent/ never re-derives or re-computes
reconciliation results itself; it only ever consumes what reconciliation/
already produced (persisted via results_store).

If the caller already provided `reconciliation_run` directly (the normal
case in tests, where a synthetic run is constructed in-memory), this is a
no-op pass-through. Otherwise it reconstructs the run from results_store's
flattened rows using `run_id`.
"""

from __future__ import annotations

import logging

from reconciliation.models import ReconciliationResult, ReconciliationRun
from results_store.reader import get_run_by_id

from agent.state import AgentState

logger = logging.getLogger(__name__)


def _reconciliation_run_from_rows(rows: list[dict]) -> ReconciliationRun:
    if not rows:
        raise ValueError("no rows found for that run_id")
    first = rows[0]
    return ReconciliationRun(
        environment=first["environment"],
        run_timestamp=first["run_timestamp"],
        trigger_type=first["trigger_type"],
        results=[
            ReconciliationResult(
                check_type=row["check_type"],
                table=row["table"],
                metric=row["metric"],
                source_value=row["source_value"],
                target_value=row["target_value"],
                diff_pct=row["diff_pct"],
                threshold=row["threshold"],
                status=row["status"],
                environment=row["environment"],
                run_timestamp=row["run_timestamp"],
            )
            for row in rows
        ],
    )


def fetch_reconciliation_results(state: AgentState) -> dict:
    if state.reconciliation_run is not None:
        logger.info("fetch_reconciliation_results: using reconciliation_run already on state")
        return {}

    if not state.run_id:
        raise ValueError("AgentState needs either reconciliation_run or run_id set")

    logger.info("fetch_reconciliation_results: fetching run_id=%s from results_store", state.run_id)
    rows = get_run_by_id(state.run_id)
    return {"reconciliation_run": _reconciliation_run_from_rows(rows)}
