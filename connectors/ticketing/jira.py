"""Jira Cloud implementation of TicketConnector, using the v3 Issue REST
API (`/rest/api/3/issue`) with HTTP Basic auth (email + API token -- the
standard Atlassian Cloud auth pattern).
"""

from __future__ import annotations

import os
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

from connectors.ticketing.base import TicketConnector


def _text_to_adf(text: str) -> dict:
    """Jira v3 requires description in Atlassian Document Format -- this
    wraps plain text as a single paragraph, which is all ADF this project
    needs."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class JiraTicketConnector(TicketConnector):
    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        default_project_key: Optional[str] = None,
    ) -> None:
        self._base_url = (base_url or os.environ["JIRA_BASE_URL"]).rstrip("/")
        self._auth = HTTPBasicAuth(
            email or os.environ["JIRA_EMAIL"],
            api_token or os.environ["JIRA_API_TOKEN"],
        )
        self._default_project_key = default_project_key or os.environ.get("JIRA_PROJECT_KEY")

    def create_ticket(self, summary: str, description: str, issue_type: str = "Bug") -> tuple[str, str]:
        if not self._default_project_key:
            raise ValueError("JIRA_PROJECT_KEY must be set in the environment")

        payload = {
            "fields": {
                "project": {"key": self._default_project_key},
                "summary": summary,
                "description": _text_to_adf(description),
                "issuetype": {"name": issue_type},
            }
        }

        response = requests.post(
            f"{self._base_url}/rest/api/3/issue",
            json=payload,
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"Jira create_ticket failed ({response.status_code}): {response.text}")

        data = response.json()
        ticket_id = data["key"]
        url = f"{self._base_url}/browse/{ticket_id}"
        return ticket_id, url
