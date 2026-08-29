"""SourceConnector interface -- the abstraction every source-system
integration (Postgres today, others later) implements. reconciliation/ and
agent/ talk only to this interface, never to a specific driver, so a new
source can be added by writing a new class here without touching either.
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


class SourceConnector(ABC):
    """Read-only access to a source system (e.g. the mock_erp tables).

    Every method takes plain table/column names and returns plain Python
    values (int, float, list[dict], list[ColumnInfo]) -- never a driver-
    specific cursor or row type. That consistency is what lets
    reconciliation/ treat any SourceConnector and any TargetConnector
    identically.
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
