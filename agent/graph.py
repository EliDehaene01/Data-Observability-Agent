"""The LangGraph reasoning graph (see CLAUDE.md):

    fetch_reconciliation_results -> analyze_diff -> classify_discrepancy
      -> [conditional branch on the (possibly downgraded) classification]
          expected      -> draft_confluence_update -> publish_docs
          needs_review  -> draft_summary -> post_to_slack_for_approval
          anomaly       -> create_jira_ticket -> notify_team

classify_discrepancy is the only LLM call in this codebase. Every other
node here is deterministic Python. The five action nodes on the three
branch endpoints are no-op stubs until phase 9 wires up real
Confluence/Jira/Slack connectors -- this phase proves the graph structure
and classification/decision logic work end-to-end.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.nodes.analyze_diff import analyze_diff
from agent.nodes.classify_discrepancy import classify_discrepancy
from agent.nodes.fetch_reconciliation_results import fetch_reconciliation_results
from agent.nodes.stub_actions import (
    create_jira_ticket,
    draft_confluence_update,
    draft_summary,
    notify_team,
    post_to_slack_for_approval,
    publish_docs,
)
from agent.state import AgentState


def _route_on_classification(state: AgentState) -> str:
    return state.final_classification


def build_graph() -> CompiledStateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("fetch_reconciliation_results", fetch_reconciliation_results)
    graph.add_node("analyze_diff", analyze_diff)
    graph.add_node("classify_discrepancy", classify_discrepancy)
    graph.add_node("draft_confluence_update", draft_confluence_update)
    graph.add_node("publish_docs", publish_docs)
    graph.add_node("draft_summary", draft_summary)
    graph.add_node("post_to_slack_for_approval", post_to_slack_for_approval)
    graph.add_node("create_jira_ticket", create_jira_ticket)
    graph.add_node("notify_team", notify_team)

    graph.add_edge(START, "fetch_reconciliation_results")
    graph.add_edge("fetch_reconciliation_results", "analyze_diff")
    graph.add_edge("analyze_diff", "classify_discrepancy")

    graph.add_conditional_edges(
        "classify_discrepancy",
        _route_on_classification,
        {
            "expected": "draft_confluence_update",
            "needs_review": "draft_summary",
            "anomaly": "create_jira_ticket",
        },
    )

    graph.add_edge("draft_confluence_update", "publish_docs")
    graph.add_edge("publish_docs", END)

    graph.add_edge("draft_summary", "post_to_slack_for_approval")
    graph.add_edge("post_to_slack_for_approval", END)

    graph.add_edge("create_jira_ticket", "notify_team")
    graph.add_edge("notify_team", END)

    return graph.compile()
