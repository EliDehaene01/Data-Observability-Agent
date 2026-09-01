"""Confluence Cloud implementation of DocsConnector, using the v1 Content
REST API (`/wiki/rest/api/content`) with HTTP Basic auth (email + API
token -- the standard Atlassian Cloud auth pattern).

Page hierarchy (see CLAUDE.md's Confluence page-hierarchy convention):
`publish_page` never creates flat top-level pages. It first ensures a parent
page exists (creating it once if missing, reusing it forever after) and
publishes the real page as a *child* of it via the `ancestors` parameter.
The parent title defaults to "Reconciliation Updates"; callers that want a
different section (e.g. the per-model docs pipeline's "Model Documentation")
pass `parent_title` explicitly.

Publishing is an upsert: if a page with the same title already exists in the
space it is updated in place (version bumped), not duplicated. Reconciliation
reports use unique per-run titles so this is effectively create-only for
them; the per-model doc pages use stable titles so re-running a PR updates
the same page.
"""

from __future__ import annotations

import html
import os
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

from connectors.docs.base import DocsConnector

RECONCILIATION_PARENT_TITLE = "Reconciliation Updates"


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

    # -- low-level REST helpers --------------------------------------------

    @property
    def _content_url(self) -> str:
        return f"{self._base_url}/wiki/rest/api/content"

    def _headers(self) -> dict:
        return {"Accept": "application/json"}

    def _find_page(self, title: str, space: str) -> Optional[dict]:
        response = requests.get(
            self._content_url,
            params={"title": title, "spaceKey": space, "expand": "version"},
            auth=self._auth,
            headers=self._headers(),
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"Confluence lookup for {title!r} failed ({response.status_code}): {response.text}"
            )
        results = response.json().get("results", [])
        return results[0] if results else None

    def _create_page(
        self, title: str, body_storage: str, space: str, ancestor_id: Optional[str]
    ) -> dict:
        payload: dict = {
            "type": "page",
            "title": title,
            "space": {"key": space},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
        }
        if ancestor_id:
            payload["ancestors"] = [{"id": ancestor_id}]

        response = requests.post(
            self._content_url,
            json=payload,
            auth=self._auth,
            headers=self._headers(),
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"Confluence create {title!r} failed ({response.status_code}): {response.text}"
            )
        return response.json()

    def _update_page(
        self, page_id: str, title: str, body_storage: str, space: str, next_version: int
    ) -> dict:
        payload = {
            "id": page_id,
            "type": "page",
            "title": title,
            "space": {"key": space},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
            "version": {"number": next_version},
        }
        response = requests.put(
            f"{self._content_url}/{page_id}",
            json=payload,
            auth=self._auth,
            headers=self._headers(),
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"Confluence update {title!r} failed ({response.status_code}): {response.text}"
            )
        return response.json()

    def _ensure_parent(self, title: str, space: str) -> str:
        """Return the id of the parent page, creating it once if it doesn't
        exist yet. Checked every call, but the create only ever runs once."""
        existing = self._find_page(title, space)
        if existing:
            return existing["id"]
        created = self._create_page(
            title,
            _text_to_storage_format(
                f"Parent page for {title}. Child pages are published here automatically "
                "by the data-observability-agent pipeline."
            ),
            space,
            ancestor_id=None,
        )
        return created["id"]

    # -- DocsConnector interface -----------------------------------------

    def publish_page(
        self,
        title: str,
        content: str,
        space_key: Optional[str] = None,
        parent_title: Optional[str] = RECONCILIATION_PARENT_TITLE,
        content_format: str = "text",
    ) -> str:
        space = space_key or self._default_space_key
        if not space:
            raise ValueError("space_key must be passed or CONFLUENCE_SPACE_KEY set in the environment")

        body_storage = content if content_format == "storage" else _text_to_storage_format(content)
        ancestor_id = self._ensure_parent(parent_title, space) if parent_title else None

        existing = self._find_page(title, space)
        if existing:
            data = self._update_page(
                existing["id"],
                title,
                body_storage,
                space,
                next_version=existing["version"]["number"] + 1,
            )
        else:
            data = self._create_page(title, body_storage, space, ancestor_id)

        links = data["_links"]
        return links["base"] + links["webui"]
