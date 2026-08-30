"""Generic container entrypoint -- run either reconciliation trigger without
assuming a GitHub Actions environment.

    run_check.py data-load    [--environment dev] [--seed] [--no-build] [--fail-on-flag]

    run_check.py code-change  --sql-diff-file F  --pr-description-file F
                              [--environment dev] [--seed] [--no-build]
                              [--run-actions] [--fail-on-flag]

The GitHub-specific scripts (scripts/run_data_load_check.py and
scripts/run_code_change_check.py) stay as the CI entrypoints -- they also
post PR comments and read PR_BASE_SHA/PR_HEAD_SHA/GITHUB_TOKEN etc. This
script shares the same reconciliation + agent code underneath, but takes
every input from CLI args / env vars so it runs identically in any
orchestrator (cron, Airflow, Argo, a plain `docker run`).

Configuration (all via environment, same names as .env.example):
  POSTGRES_CONNECTION_STRING   source ERP (required)
  DUCKDB_PATH                  dbt warehouse file (required; built here unless --no-build)
  RESULTS_STORE_PATH           append-only run history (optional; default results_store/results.duckdb)
  ANTHROPIC_API_KEY            required by `code-change` when a check is flagged
  CONFLUENCE_* / JIRA_* / SLACK_WEBHOOK_URL   required by `code-change --run-actions`
  TARGET_ENVIRONMENT           default for --environment (default: dev)
  SEED_NUM_ORDERS              row volume for --seed (default: 3000)
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:  # optional: real orchestrators inject env vars directly
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

from agent.graph import build_graph  # noqa: E402
from agent.nodes.analyze_diff import analyze_diff  # noqa: E402
from agent.nodes.classify_discrepancy import classify_discrepancy  # noqa: E402
from agent.state import AgentState  # noqa: E402
from connectors.source.postgres_source import PostgresSourceConnector  # noqa: E402
from connectors.target.duckdb_target import DuckDBTargetConnector  # noqa: E402
from reconciliation.aggregate_checks import run_aggregate_checks  # noqa: E402
from reconciliation.models import ReconciliationRun  # noqa: E402
from reconciliation.sample_checks import run_sample_checks  # noqa: E402
from results_store.writer import write_run  # noqa: E402


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, env=os.environ)


def _prepare_target(seed: bool, build: bool) -> None:
    """Seed the source and/or build the dbt warehouse -- the same EL + `dbt
    run` steps the CI workflows do inline. Skip with --no-build if the
    orchestrator already produced DUCKDB_PATH."""
    if seed:
        num_orders = os.environ.get("SEED_NUM_ORDERS", "3000")
        _run([sys.executable, "mock_erp/seed_data.py", "--engine", "postgres", "--num-orders", num_orders])
    if build:
        _run([sys.executable, "scripts/load_source_into_duckdb.py"])
        _run([sys.executable, "-c", "from dbt.cli.main import cli; cli()", "run",
              "--project-dir", "dbt_project", "--profiles-dir", "dbt_project"])


def _reconcile(environment: str, trigger_type: str) -> ReconciliationRun:
    source = PostgresSourceConnector()
    target = DuckDBTargetConnector()
    try:
        results = run_aggregate_checks(source, target, environment) + run_sample_checks(
            source, target, environment
        )
    finally:
        source.close()
        target.close()
    return ReconciliationRun(
        environment=environment,
        run_timestamp=datetime.now(timezone.utc),
        trigger_type=trigger_type,
        results=results,
    )


def _print_results(run: ReconciliationRun) -> list:
    flagged = [r for r in run.results if r.status == "flag"]
    print(f"Reconciliation: {len(run.results)} results, {len(flagged)} flagged.")
    for r in run.results:
        marker = "FLAG" if r.status == "flag" else "pass"
        print(
            f"  [{marker}] {r.check_type:9s} {r.table:30s} {r.metric:20s} "
            f"diff_pct={r.diff_pct:.2f} threshold={r.threshold:.2f}"
        )
    return flagged


def _results_store_path() -> str | None:
    return os.environ.get("RESULTS_STORE_PATH") or None


def _read_input(inline: str | None, file_path: str | None, env_var: str) -> str:
    if file_path:
        return Path(file_path).read_text()
    if inline is not None:
        return inline
    return os.environ.get(env_var, "")


def cmd_data_load(args: argparse.Namespace) -> int:
    _prepare_target(seed=args.seed, build=args.build)
    run = _reconcile(args.environment, "data_load")
    flagged = _print_results(run)
    run_id = write_run(run, db_path=_results_store_path())
    print(f"Wrote run_id={run_id} to results_store ({len(flagged)} flagged).")
    return 1 if (flagged and args.fail_on_flag) else 0


def cmd_code_change(args: argparse.Namespace) -> int:
    _prepare_target(seed=args.seed, build=args.build)
    run = _reconcile(args.environment, "code_change")
    flagged = _print_results(run)

    final_classification = confidence = pr_claims_no_impact = downgraded = None

    if flagged:
        sql_diff = _read_input(args.sql_diff, args.sql_diff_file, "SQL_DIFF")
        pr_description = _read_input(args.pr_description, args.pr_description_file, "PR_DESCRIPTION")
        if not sql_diff:
            print("warning: no SQL diff supplied -- classify_discrepancy has no code context to reason over")

        state = AgentState(reconciliation_run=run, sql_diff=sql_diff, pr_description=pr_description)

        if args.run_actions:
            # Full LangGraph flow, including the Confluence/Jira/Slack action
            # nodes on each branch -- needs those connectors' env vars set.
            final_state = AgentState(**build_graph().invoke(state))
        else:
            # Reconciliation + classification only: run the deterministic
            # analyze_diff node and the one LLM node, and stop before the
            # side-effecting action branch. This is the safe default -- a
            # container consumer gets the classification and the
            # results_store row without needing Confluence/Jira/Slack wired
            # up. Opt into the rest with --run-actions.
            state.diff_touched_tables = analyze_diff(state)["diff_touched_tables"]
            final_state = AgentState(**{**state.model_dump(), **classify_discrepancy(state)})

        raw = final_state.classification
        final_classification = final_state.final_classification
        confidence = raw.confidence
        pr_claims_no_impact = raw.pr_claims_no_impact
        downgraded = final_state.downgraded
        print(
            f"classify_discrepancy: final={final_classification} confidence={confidence:.2f} "
            f"downgraded={downgraded} (llm raw={raw.classification})"
        )
    else:
        print("No flagged results -- skipping classify_discrepancy, nothing to classify.")

    run_id = write_run(
        run,
        db_path=_results_store_path(),
        final_classification=final_classification,
        confidence=confidence,
        pr_claims_no_impact=pr_claims_no_impact,
        downgraded=downgraded,
    )
    print(f"Wrote run_id={run_id} to results_store.")
    return 1 if (flagged and args.fail_on_flag) else 0


def _build_parser() -> argparse.ArgumentParser:
    default_env = os.environ.get("TARGET_ENVIRONMENT", "dev")
    parser = argparse.ArgumentParser(prog="run_check.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("data-load", "code-change"):
        p = sub.add_parser(name)
        p.add_argument("--environment", default=default_env, help=f"dev/qa/prd (default: {default_env})")
        p.add_argument("--seed", action="store_true",
                       help="seed the Postgres source first (demo / throwaway envs only)")
        p.add_argument("--no-build", dest="build", action="store_false",
                       help="skip the EL + `dbt run` step (DUCKDB_PATH already built by the orchestrator)")
        p.add_argument("--fail-on-flag", action="store_true",
                       help="exit non-zero if any reconciliation check is flagged")

    cc = sub.choices["code-change"]
    cc.add_argument("--sql-diff-file", help="path to a file containing the dbt models SQL diff")
    cc.add_argument("--sql-diff", help="the SQL diff inline (alternative to --sql-diff-file)")
    cc.add_argument("--pr-description-file", help="path to a file containing the PR description")
    cc.add_argument("--pr-description", help="the PR description inline")
    cc.add_argument("--run-actions", action="store_true",
                    help="also run the Confluence/Jira/Slack action nodes (needs their env vars)")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "data-load":
        return cmd_data_load(args)
    return cmd_code_change(args)


if __name__ == "__main__":
    raise SystemExit(main())
