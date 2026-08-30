"""Confluence Cloud implementation of DocsConnector, using the v1 Content
REST API (`/wiki/rest/api/content`) with HTTP Basic auth (email + API
token -- the standard Atlassian Cloud auth pattern).
"""

from __future__ import annotations

import html
import os
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

from connectors.docs.base import DocsConnector


def _text_to_storage_format(content: str) -> str:
    """Confluence's storage format is XHTML-like. Plain text is escaped and
    each line becomes its own paragraph so line breaks survive."""
    paragraphs = content.split("\n")
    return "".join(f"<p>{html.escape(line)}</p>" for line in paragraphs if line.strip())


class ConfluenceDocsConnector(DocsConnector):
    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        default_space_key: Optional[str] = None,
    ) -> None:
        self._base_url = (base_url or os.environ["CONFLUENCE_BASE_URL"]).rstrip("/")
        self._auth = HTTPBasicAuth(
            email or os.environ["CONFLUENCE_EMAIL"],
            api_token or os.environ["CONFLUENCE_API_TOKEN"],
        )
        self._default_space_key = default_space_key or os.environ.get("CONFLUENCE_SPACE_KEY")

    def publish_page(self, title: str, content: str, space_key: Optional[str] = None) -> str:
        space = space_key or self._default_space_key
        if not space:
            raise ValueError("space_key must be passed or CONFLUENCE_SPACE_KEY set in the environment")

        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space},
            "body": {
                "storage": {
                    "value": _text_to_storage_format(content),
                    "representation": "storage",
                }
            },
        }

        response = requests.post(
            f"{self._base_url}/wiki/rest/api/content",
            json=payload,
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"Confluence publish_page failed ({response.status_code}): {response.text}")

        data = response.json()
        links = data["_links"]
        return links["base"] + links["webui"]
