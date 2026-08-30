"""ReportingConnector interface -- the abstraction every dashboard/reporting
integration (a static HTML page today, something else like Power BI or
Metabase later) implements.

Per CLAUDE.md's dashboard boundary rule: implementations read only from
results_store, never from live reconciliation output or agent state
directly. This keeps the dashboard decoupled from whichever
reconciliation/agent internals change later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ReportingConnector(ABC):
    @abstractmethod
    def generate_report(self, output_path: str) -> None:
        """Write a self-contained report to output_path, reading only from
        results_store."""
        raise NotImplementedError
