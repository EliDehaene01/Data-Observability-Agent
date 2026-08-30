"""CI entry point for the PR-triggered code-change validation trigger
(.github/workflows/on_dbt_change.yml). Orchestrates the existing
reconciliation + agent/graph.py pipeline -- no new classification logic
lives here (see CLAUDE.md); this script only wires together pieces that
already exist.

Reads its inputs from the environment (all set by the workflow step):
  TARGET_ENVIRONMENT  -- dev/qa/prd, mapped from the PR's base branch
  PR_BASE_SHA         -- base commit of the diff
  PR_HEAD_SHA         -- head commit of the diff
  PR_DESCRIPTION      -- the PR body (github.event.pull_request.body)
  PR_NUMBER           -- for posting the summary comment
  GITHUB_TOKEN        -- built-in token, pull-requests: write permission
  GITHUB_REPOSITORY, GITHUB_API_URL -- set automatically by Actions
plus the usual POSTGRES_CONNECTION_STRING / DUCKDB_PATH / ANTHROPIC_API_KEY
/ CONFLUENCE_*/JIRA_*/SLACK_WEBHOOK_URL used by the connectors underneath.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

import requests

from agent.graph import build_graph
from agent.state import AgentState
from connectors.source.postgres_source import PostgresSourceConnector
from connectors.target.duckdb_target import DuckDBTargetConnector
from reconciliation.aggregate_checks import run_aggregate_checks
from reconciliation.models import ReconciliationRun
from reconciliation.sample_checks import run_sample_checks
from results_store.writer import write_run

MODELS_PATH = "dbt_project/models"


def _get_sql_diff(base_sha: str, head_sha: str) -> str:
    result = subprocess.run(
        ["git", "diff", base_sha, head_sha, "--", MODELS_PATH],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _post_pr_comment(body: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    token = os.environ["GITHUB_TOKEN"]

    response = requests.post(
        f"{api_url}/repos/{repo}/issues/{pr_number}/comments",
        json={"body": body},
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Posting PR comment failed ({response.status_code}): {response.text}")


def _format_comment(state: AgentState) -> str:
    classification = state.classification
    lines = [
        "## Data Observability Agent — reconciliation classification",
        "",
        f"**Environment:** {state.reconciliation_run.environment}",
        f"**Classification:** `{state.final_classification}`"
        + (f" (downgraded from `{classification.classification}`)" if state.downgraded else ""),
        f"**Confidence:** {classification.confidence:.2f}",
        f"**PR claims no impact:** {classification.pr_claims_no_impact}",
        "",
        "**Reasoning:**",
        classification.reasoning,
    ]
    return "\n".join(lines)


def _format_no_divergence_comment(environment: str) -> str:
    return (
        "## Data Observability Agent — reconciliation classification\n\n"
        f"**Environment:** {environment}\n\n"
        "No reconciliation checks were flagged for this change — nothing to classify."
    )


def main() -> None:
    environment = os.environ.get("TARGET_ENVIRONMENT", "dev")

    source = PostgresSourceConnector()
    target = DuckDBTargetConnector()
    try:
        results = run_aggregate_checks(source, target, environment) + run_sample_checks(
            source, target, environment
        )
    finally:
        source.close()
        target.close()

    run = ReconciliationRun(
        environment=environment,
        run_timestamp=datetime.now(timezone.utc),
        trigger_type="code_change",
        results=results,
    )

    flagged = [r for r in run.results if r.status == "flag"]
    print(f"Reconciliation: {len(run.results)} results, {len(flagged)} flagged.")
    for r in run.results:
        marker = "FLAG" if r.status == "flag" else "pass"
        print(
            f"  [{marker}] {r.check_type:9s} {r.table:30s} {r.metric:20s} "
            f"diff_pct={r.diff_pct:.2f} threshold={r.threshold:.2f}"
        )

    if flagged:
        sql_diff = _get_sql_diff(os.environ["PR_BASE_SHA"], os.environ["PR_HEAD_SHA"])
        pr_description = os.environ.get("PR_DESCRIPTION") or ""

        state = AgentState(
            reconciliation_run=run,
            sql_diff=sql_diff,
            pr_description=pr_description,
        )
        result = build_graph().invoke(state)
        final_state = AgentState(**result)

        print(
            f"classify_discrepancy: final={final_state.final_classification} "
            f"confidence={final_state.classification.confidence:.2f} "
            f"downgraded={final_state.downgraded}"
        )
        comment_body = _format_comment(final_state)
    else:
        print("No flagged results — skipping classify_discrepancy, nothing to classify.")
        comment_body = _format_no_divergence_comment(environment)

    _post_pr_comment(comment_body)

    run_id = write_run(run)
    print(f"Wrote run_id={run_id} to results_store.")


if __name__ == "__main__":
    main()
