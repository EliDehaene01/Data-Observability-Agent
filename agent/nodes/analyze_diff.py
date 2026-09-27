"""analyze_diff -- deterministic pre-processing, no LLM call. Extracts which
of the tables involved in reconciliation_run's *flagged* results are
literally mentioned in the SQL diff text. This is grounding signal for
classify_discrepancy: a flagged table the diff never touches at all is
strong (deterministic) evidence against "expected", regardless of how the
PR description reads.

Only flagged results count. A passing check can't be part of the
discrepancy being explained, so a diff that merely touches its table (e.g.
a comment in landing_vbak.sql while the passing vbak -> landing_vbak check
sits in the same run) must not make the PR-honesty override in
classify_discrepancy eligible to fire -- see CLAUDE.md.
"""

from __future__ import annotations

import logging
import re

from agent.state import AgentState

logger = logging.getLogger(__name__)


_TABLE_NAME = re.compile(r"\s*([A-Za-z0-9_]+)")


def _flagged_tables_in_run(state: AgentState) -> set[str]:
    tables: set[str] = set()
    for result in state.reconciliation_run.results:
        # Same "actually exceeded its threshold" test the override itself
        # uses (classify_discrepancy._has_uncorroborated_flagged_result).
        if result.diff_pct <= result.threshold:
            continue
        # `table` is formatted as "source -> target_table", where a filtered
        # source reads e.g. "vbap (excl. cancelled)" (see
        # reconciliation/aggregate_checks.py) -- keep just the leading name.
        for side in result.table.split(" -> "):
            match = _TABLE_NAME.match(side)
            if match:
                tables.add(match.group(1))
    return tables


def analyze_diff(state: AgentState) -> dict:
    flagged_tables = _flagged_tables_in_run(state)
    touched = sorted(table for table in flagged_tables if table in state.sql_diff)
    logger.info("analyze_diff: diff mentions %s out of flagged %s", touched, sorted(flagged_tables))
    return {"diff_touched_tables": touched}
