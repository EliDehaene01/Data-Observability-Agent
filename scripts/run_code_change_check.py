"""CI entry point for the PR-triggered code-change validation trigger
(.github/workflows/on_dbt_change.yml). Orchestrates the existing
reconciliation + agent/graph.py pipeline -- no new classification logic
lives here (see CLAUDE.md); this script only wires together pieces that
already exist.

Reads its inputs from the environment (all set by the workflow step):
  GITHUB_EVENT_NAME   -- "pull_request" or "workflow_dispatch" (set by Actions)
  TARGET_ENVIRONMENT  -- dev/qa/prd, mapped from the PR's base branch (or the
                         workflow_dispatch `environment` input)
  PR_BASE_SHA         -- base commit of the diff        (pull_request only)
  PR_HEAD_SHA         -- head commit of the diff        (pull_request only)
  PR_DESCRIPTION      -- the PR body                    (pull_request only)
  PR_NUMBER           -- for posting the summary comment (pull_request only)
  SYNTHETIC_SQL_DIFF        -- manual synthetic diff    (workflow_dispatch only)
  SYNTHETIC_PR_DESCRIPTION  -- manual PR description     (workflow_dispatch only)
  GITHUB_TOKEN        -- built-in token, pull-requests: write permission
  GITHUB_OUTPUT       -- Actions step-output file; `blocking` + `final_classification`
                         are written here for the PR-blocking policy step
  GITHUB_REPOSITORY, GITHUB_API_URL -- set automatically by Actions
plus the usual POSTGRES_CONNECTION_STRING / DUCKDB_PATH / ANTHROPIC_API_KEY
/ CONFLUENCE_*/JIRA_*/SLACK_WEBHOOK_URL used by the connectors underneath.

In workflow_dispatch mode there is no real PR: the synthetic inputs are used
directly and the summary is logged instead of posted as a PR comment.
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

# A final_classification in this set makes the GitHub check fail (see the
# "Enforce PR-blocking policy" step in on_dbt_change.yml). "expected" is the
# only non-blocking outcome -- anything the agent flags for a human, or
# tickets outright, should block the merge.
BLOCKING_CLASSIFICATIONS = {"needs_review", "anomaly"}


def _is_workflow_dispatch() -> bool:
    """True when the workflow was triggered manually (workflow_dispatch) with
    a synthetic diff + PR description, rather than by a real pull_request
    event. In that mode there is no PR to diff against or comment on."""
    return os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"


def _write_github_output(**pairs: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in pairs.items():
            handle.write(f"{key}={value}\n")


def _get_sql_diff(base_sha: str, head_sha: str) -> str:
    result = subprocess.run(
        ["git", "diff", base_sha, head_sha, "--", MODELS_PATH],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _emit_comment(body: str) -> None:
    """Post the summary as a PR comment -- unless this is a workflow_dispatch
    run, where there is no PR. In that mode the would-be comment is logged
    instead (per the task: skip the real PR-comment step, log it)."""
    if _is_workflow_dispatch():
        print("workflow_dispatch mode: no PR to comment on. Would have posted:\n")
        print(body)
        return
    _post_pr_comment(body)


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

    final_classification = confidence = pr_claims_no_impact = downgraded = None

    if flagged:
        if _is_workflow_dispatch():
            # Manual run: use the synthetic inputs directly, don't derive
            # them from a (non-existent) PR event.
            sql_diff = os.environ.get("SYNTHETIC_SQL_DIFF", "")
            pr_description = os.environ.get("SYNTHETIC_PR_DESCRIPTION", "")
        else:
            sql_diff = _get_sql_diff(os.environ["PR_BASE_SHA"], os.environ["PR_HEAD_SHA"])
            pr_description = os.environ.get("PR_DESCRIPTION") or ""

        state = AgentState(
            reconciliation_run=run,
            sql_diff=sql_diff,
            pr_description=pr_description,
        )
        result = build_graph().invoke(state)
        final_state = AgentState(**result)

        final_classification = final_state.final_classification
        confidence = final_state.classification.confidence
        pr_claims_no_impact = final_state.classification.pr_claims_no_impact
        downgraded = final_state.downgraded

        print(
            f"classify_discrepancy: final={final_classification} "
            f"confidence={confidence:.2f} downgraded={downgraded}"
        )
        comment_body = _format_comment(final_state)
    else:
        print("No flagged results — skipping classify_discrepancy, nothing to classify.")
        comment_body = _format_no_divergence_comment(environment)

    _emit_comment(comment_body)

    run_id = write_run(
        run,
        final_classification=final_classification,
        confidence=confidence,
        pr_claims_no_impact=pr_claims_no_impact,
        downgraded=downgraded,
    )
    print(f"Wrote run_id={run_id} to results_store.")

    # Signal the PR-blocking policy to the workflow. The results_store write
    # above has already happened, so the audit trail is safe regardless of
    # what the workflow does with this: it fails the check *after* committing
    # results, never instead of it.
    blocking = final_classification in BLOCKING_CLASSIFICATIONS
    _write_github_output(
        final_classification=final_classification or "none",
        blocking="true" if blocking else "false",
    )
    print(
        f"PR-blocking policy: blocking={blocking} "
        f"(final_classification={final_classification or 'none'})"
    )


if __name__ == "__main__":
    main()
