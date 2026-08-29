"""Postgres implementation of SourceConnector, using psycopg2 against
POSTGRES_CONNECTION_STRING (see .env.example).
"""

from __future__ import annotations

import os
from typing import Any

import psycopg2

from connectors.source.base import (
    COMPARISON_OPERATORS,
    SUPPORTED_AGG_FUNCS,
    ColumnInfo,
    Filters,
    SourceConnector,
)


class PostgresSourceConnector(SourceConnector):
    def __init__(self, connection_string: str | None = None) -> None:
        self._connection_string = connection_string or os.environ["POSTGRES_CONNECTION_STRING"]
        self._conn = psycopg2.connect(self._connection_string)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PostgresSourceConnector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _build_where(self, filters: Filters | None) -> tuple[str, list[Any]]:
        if not filters:
            return "", []
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in filters.items():
            operator, operand = value if isinstance(value, tuple) else ("=", value)
            if operator not in COMPARISON_OPERATORS:
                raise ValueError(f"Unsupported filter operator: {operator!r}")
            clauses.append(f"{self._quote_ident(column)} {operator} %s")
            params.append(operand)
        return " where " + " and ".join(clauses), params

    def get_row_count(self, table: str, filters: Filters | None = None) -> int:
        where_sql, params = self._build_where(filters)
        query = f"select count(*) from {self._quote_ident(table)}{where_sql}"
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return int(cur.fetchone()[0])

    def get_aggregate(
        self, table: str, column: str, agg_func: str, filters: Filters | None = None
    ) -> float:
        if agg_func not in SUPPORTED_AGG_FUNCS:
            raise ValueError(f"Unsupported aggregate function: {agg_func!r}")
        where_sql, params = self._build_where(filters)
        query = f"select {agg_func}({self._quote_ident(column)}) from {self._quote_ident(table)}{where_sql}"
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchone()[0]
            return float(result) if result is not None else 0.0

    def sample_rows(
        self, table: str, n: int, filters: Filters | None = None
    ) -> list[dict[str, Any]]:
        where_sql, params = self._build_where(filters)
        query = f"select * from {self._quote_ident(table)}{where_sql} limit %s"
        with self._conn.cursor() as cur:
            cur.execute(query, [*params, n])
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def get_schema(self, table: str) -> list[ColumnInfo]:
        query = """
            select column_name, data_type
            from information_schema.columns
            where table_name = %s
            order by ordinal_position
        """
        with self._conn.cursor() as cur:
            cur.execute(query, [table])
            return [ColumnInfo(name=row[0], data_type=row[1]) for row in cur.fetchall()]
