"""Shared pytest fixtures and configuration.

Real Postgres/DuckDB connectors, reusing whatever mock_erp seed data is
already sitting in the local Postgres instance -- this bootstraps it if
empty, but never destructively reseeds existing data. The DuckDB target is
built fresh (once per test session) into a temp file via the same
seed-into-Postgres -> load-into-DuckDB -> dbt-run pipeline the real
workflows use, so tests exercise the actual landing/prep/serve models, not
a stand-in.

See tests/README.md for how to run the full suite, including the small
number of @pytest.mark.live tests (real Anthropic API calls) that are
skipped by default.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# The connector tests hit real local Postgres/DuckDB; their connection
# settings live in .env (see tests/README.md). Nothing else in the project
# auto-loads it (the CI workflows pass env vars explicitly), so do it here.
load_dotenv(REPO_ROOT / ".env")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="also run tests marked @pytest.mark.live (real Anthropic API calls)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: calls the real Anthropic API -- skipped by default, use --run-live"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="needs --run-live to call the real Anthropic API")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def postgres_source():
    """A real PostgresSourceConnector against the local mock_erp database.
    Bootstraps the seed data if the tables are empty/missing; otherwise
    reuses whatever is already there untouched."""
    from connectors.source.postgres_source import PostgresSourceConnector

    conn = PostgresSourceConnector()
    try:
        row_count = conn.get_row_count("vbak")
    except Exception:
        row_count = 0

    if row_count == 0:
        conn.close()
        from mock_erp.seed_data import _PsycopgExecuteAdapter, seed

        pg_conn = psycopg2.connect(os.environ["POSTGRES_CONNECTION_STRING"])
        try:
            seed(_PsycopgExecuteAdapter(pg_conn))
        finally:
            pg_conn.close()
        conn = PostgresSourceConnector()

    yield conn
    conn.close()


@pytest.fixture(scope="session")
def built_duckdb_path(tmp_path_factory: pytest.TempPathFactory, postgres_source) -> str:
    """A DuckDB file with the full landing/prep/serve pipeline built against
    the (already-seeded, via the postgres_source fixture) Postgres data --
    same EL + dbt run steps the real workflows use, built once per session
    into an isolated temp file so tests never touch a developer's own
    dev.duckdb."""
    db_path = tmp_path_factory.mktemp("dbt_target") / "test_target.duckdb"
    env = dict(os.environ, DUCKDB_PATH=str(db_path))

    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "load_source_into_duckdb.py")],
        check=True,
        cwd=REPO_ROOT,
        env=env,
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from dbt.cli.main import cli; cli()",
            "run",
            "--project-dir",
            "dbt_project",
            "--profiles-dir",
            "dbt_project",
        ],
        check=True,
        cwd=REPO_ROOT,
        env=env,
    )
    return str(db_path)


@pytest.fixture(scope="session")
def duckdb_target(built_duckdb_path: str):
    """A real DuckDBTargetConnector against the session's built target."""
    from connectors.target.duckdb_target import DuckDBTargetConnector

    conn = DuckDBTargetConnector(db_path=built_duckdb_path)
    yield conn
    conn.close()
