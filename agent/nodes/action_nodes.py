"""Action nodes on the three classification branches. draft_confluence_update
and draft_summary still just build text (deterministic, no external call);
publish_docs, create_jira_ticket, and post_slack_notification are the real
connector calls -- see CLAUDE.md's connector abstraction pattern.

notify_team remains a log-only placeholder: this phase wires up
Confluence/Jira/Slack specifically (per the task), not a second Slack call
for internal team notifications.

post_slack_notification (formerly post_to_slack_for_approval) is a plain,
one-way Slack message -- see connectors/ticketing/slack.py's docstring for
why real interactive human-in-the-loop approval isn't implemented yet.
"""

from __future__ import annotations

import logging
import os
import uuid

from connectors.docs.confluence import RECONCILIATION_PARENT_TITLE, ConfluenceDocsConnector
from connectors.ticketing.jira import JiraTicketConnector
from connectors.ticketing.slack import notify

from agent.state import AgentState

logger = logging.getLogger(__name__)


def draft_confluence_update(state: AgentState) -> dict:
    doc = (
        f"Reconciliation discrepancy in {state.reconciliation_run.environment} "
        f"classified as expected (confidence={state.classification.confidence:.2f}).\n\n"
        f"Reasoning: {state.classification.reasoning}"
    )
    logger.info("draft_confluence_update: %s", doc)
    return {"confluence_doc": doc}


def publish_docs(state: AgentState) -> dict:
    # Confluence rejects a duplicate title within a space. Second-level
    # timestamp precision alone isn't enough -- two runs (or two retries)
    # can land in the same second -- so a short random suffix guarantees
    # uniqueness regardless of timing.
    title = (
        f"Reconciliation update - {state.reconciliation_run.environment} - "
        f"{state.reconciliation_run.run_timestamp:%Y-%m-%d %H:%M:%S UTC} - {uuid.uuid4().hex[:8]}"
    )
    url = ConfluenceDocsConnector().publish_page(
        title=title,
        content=state.confluence_doc,
        parent_title=RECONCILIATION_PARENT_TITLE,
    )
    logger.info("publish_docs: published %r -> %s", title, url)
    return {}


def draft_summary(state: AgentState) -> dict:
    message = (
        f"Reconciliation discrepancy in {state.reconciliation_run.environment} needs human review "
        f"(llm classification={state.classification.classification}, "
        f"confidence={state.classification.confidence:.2f}"
        f"{', downgraded from expected' if state.downgraded else ''}).\n"
        f"Reasoning: {state.classification.reasoning}"
    )
    logger.info("draft_summary: %s", message)
    return {"slack_message": message}


def post_slack_notification(state: AgentState) -> dict:
    notify(state.slack_message)
    logger.info("post_slack_notification: posted to Slack (plain notification, no approval mechanism)")
    return {}


def create_jira_ticket(state: AgentState) -> dict:
    summary = f"Reconciliation anomaly: {state.reconciliation_run.environment}"
    description = (
        f"Anomaly in reconciliation for {state.reconciliation_run.environment} "
        f"(confidence={state.classification.confidence:.2f}).\n\n"
        f"Reasoning: {state.classification.reasoning}"
    )
    # JIRA_ISSUE_TYPE lets a specific Jira site override the connector's
    # "Bug" default -- e.g. this project's real test site has a localized
    # issue-type scheme with no "Bug" type at all (see .env's comment).
    issue_type = os.environ.get("JIRA_ISSUE_TYPE", "Bug")
    ticket_id, url = JiraTicketConnector().create_ticket(
        summary=summary, description=description, issue_type=issue_type
    )
    logger.info("create_jira_ticket: created %s -> %s", ticket_id, url)
    return {"jira_ticket": f"{ticket_id}: {url}"}


def notify_team(state: AgentState) -> dict:
    logger.info("notify_team: [stub] would notify team of new ticket:\n%s", state.jira_ticket)
    return {}
