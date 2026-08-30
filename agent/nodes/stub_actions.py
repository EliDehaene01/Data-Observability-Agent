"""No-op placeholder action nodes. Real Confluence/Jira/Slack connectors
come in a later phase (see CLAUDE.md's connectors/{docs,ticketing} and
TODO.md item 9) -- these just log what they would do, so the graph
structure and classification logic can be proven end-to-end without any
external integration existing yet. No LLM calls belong here either.
"""

from __future__ import annotations

import logging

from agent.state import AgentState

logger = logging.getLogger(__name__)


def draft_confluence_update(state: AgentState) -> dict:
    doc = (
        f"[Confluence draft] Reconciliation discrepancy in "
        f"{state.reconciliation_run.environment} classified as expected "
        f"(confidence={state.classification.confidence:.2f}).\nReasoning: "
        f"{state.classification.reasoning}"
    )
    logger.info("draft_confluence_update: %s", doc)
    return {"confluence_doc": doc}


def publish_docs(state: AgentState) -> dict:
    logger.info("publish_docs: [stub] would publish to Confluence:\n%s", state.confluence_doc)
    return {}


def draft_summary(state: AgentState) -> dict:
    message = (
        f"[Slack draft] Reconciliation discrepancy in "
        f"{state.reconciliation_run.environment} needs human review "
        f"(llm classification={state.classification.classification}, "
        f"confidence={state.classification.confidence:.2f}"
        f"{', downgraded from expected' if state.downgraded else ''}).\n"
        f"Reasoning: {state.classification.reasoning}"
    )
    logger.info("draft_summary: %s", message)
    return {"slack_message": message}


def post_to_slack_for_approval(state: AgentState) -> dict:
    logger.info(
        "post_to_slack_for_approval: [stub] would post for human-in-the-loop "
        "approval and pause here:\n%s",
        state.slack_message,
    )
    return {}


def create_jira_ticket(state: AgentState) -> dict:
    ticket = (
        f"[Jira draft] Anomaly in reconciliation for "
        f"{state.reconciliation_run.environment} "
        f"(confidence={state.classification.confidence:.2f}).\n"
        f"Reasoning: {state.classification.reasoning}"
    )
    logger.info("create_jira_ticket: %s", ticket)
    return {"jira_ticket": ticket}


def notify_team(state: AgentState) -> dict:
    logger.info("notify_team: [stub] would notify team of new ticket:\n%s", state.jira_ticket)
    return {}
