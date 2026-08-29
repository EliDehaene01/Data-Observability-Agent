"""TargetConnector interface -- deliberately the same method shape as
SourceConnector (connectors/source/base.py), so reconciliation/ can compare
a source and a target through identical calls without knowing which engine
sits behind either one. Kept as a separate, self-contained interface rather
than importing from connectors/source/ so the two stay independently
swappable, per CLAUDE.md's connector abstraction rule.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Union

SUPPORTED_AGG_FUNCS = frozenset({"sum", "avg", "count", "min", "max"})
COMPARISON_OPERATORS = frozenset({"=", "!=", ">", ">=", "<", "<="})

# A filter value is either a scalar (implies "=") or an (operator, value) pair,
# e.g. {"status": "completed", "order_date": (">=", date(2026, 1, 1))}.
FilterValue = Union[Any, tuple[str, Any]]
Filters = dict[str, FilterValue]


@dataclass(frozen=True)
class ColumnInfo:
    """One column of a table's schema, in a shape common to every connector."""

    name: str
    data_type: str


class TargetConnector(ABC):
    """Read-only access to a target warehouse (e.g. the dbt_project serve/prep models).

    Every method takes plain table/column names and returns plain Python
    values (int, float, list[dict], list[ColumnInfo]) -- never a driver-
    specific cursor or row type, mirroring SourceConnector exactly.
    """

    @abstractmethod
    def get_row_count(self, table: str, filters: Filters | None = None) -> int:
        """Number of rows in `table` matching `filters`."""
        raise NotImplementedError

    @abstractmethod
    def get_aggregate(
        self, table: str, column: str, agg_func: str, filters: Filters | None = None
    ) -> float:
        """Aggregate (`agg_func` must be one of SUPPORTED_AGG_FUNCS) of `column`
        in `table` matching `filters`."""
        raise NotImplementedError

    @abstractmethod
    def sample_rows(
        self, table: str, n: int, filters: Filters | None = None
    ) -> list[dict[str, Any]]:
        """Up to `n` rows from `table` matching `filters`, each a column-name -> value dict."""
        raise NotImplementedError

    @abstractmethod
    def get_schema(self, table: str) -> list[ColumnInfo]:
        """Column names and data types for `table`."""
        raise NotImplementedError

    @staticmethod
    def _quote_ident(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'
