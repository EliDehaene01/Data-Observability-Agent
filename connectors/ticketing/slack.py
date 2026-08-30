"""Slack notification -- deliberately NOT a TicketConnector and NOT an
approval workflow. Posts a one-way message to SLACK_WEBHOOK_URL and
returns; there is no mechanism here to receive a human's approve/reject
response.

Real interactive approval (buttons a human clicks, a response the graph
resumes on) would require a hosted callback endpoint -- Slack's
interactivity request URL posts back to a server you run, which this
project doesn't have. That's out of scope for this MVP; see
agent/nodes/action_nodes.py's post_slack_notification for where this is
called and why it's named to avoid implying real HITL exists yet.
"""

from __future__ import annotations

import os
from typing import Optional

import requests


def notify(message: str, channel: Optional[str] = None) -> None:
    webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    payload: dict = {"text": message}
    if channel:
        # Most Slack incoming webhooks are locked to the channel chosen at
        # creation time and silently ignore this -- included for the rare
        # legacy webhook that still honors an override.
        payload["channel"] = channel

    response = requests.post(webhook_url, json=payload, timeout=10)
    if not response.ok:
        raise RuntimeError(f"Slack notify failed ({response.status_code}): {response.text}")
