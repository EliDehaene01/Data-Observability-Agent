"""analyze_diff -- deterministic pre-processing, no LLM call. Extracts which
of the tables involved in reconciliation_run's results are literally
mentioned in the SQL diff text. This is grounding signal for
classify_discrepancy: a flagged table the diff never touches at all is
strong (deterministic) evidence against "expected", regardless of how the
PR description reads.
"""

from __future__ import annotations

import logging

from agent.state import AgentState

logger = logging.getLogger(__name__)


def _tables_in_run(state: AgentState) -> set[str]:
    tables: set[str] = set()
    for result in state.reconciliation_run.results:
        # `table` is formatted as "source_table -> target_table" (see
        # reconciliation/aggregate_checks.py / sample_checks.py).
        for name in result.table.split(" -> "):
            tables.add(name.strip())
    return tables


def analyze_diff(state: AgentState) -> dict:
    known_tables = _tables_in_run(state)
    touched = sorted(table for table in known_tables if table in state.sql_diff)
    logger.info("analyze_diff: diff mentions %s out of %s", touched, sorted(known_tables))
    return {"diff_touched_tables": touched}
