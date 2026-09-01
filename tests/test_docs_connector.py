"""Tests for connectors/docs/confluence.py's page-hierarchy + upsert
behavior, with the HTTP layer (`requests`) mocked -- no real Confluence.

Covers:
  - a missing parent page is created once, then the real page is published
    as its child (ancestors parameter)
  - an existing parent page is reused, not recreated
  - an existing page of the same title is updated in place (version bumped),
    not duplicated
  - content_format="storage" is published verbatim; "text" is wrapped
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from connectors.docs.confluence import ConfluenceDocsConnector


@pytest.fixture(autouse=True)
def _confluence_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("CONFLUENCE_EMAIL", "bot@example.com")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "token-not-used")
    monkeypatch.setenv("CONFLUENCE_SPACE_KEY", "SD")


def _resp(payload, ok=True, status=200):
    return SimpleNamespace(ok=ok, status_code=status, json=lambda: payload, text="")


def _empty_search():
    return _resp({"results": []})


def _found(page_id, version=1):
    return _resp({"results": [{"id": page_id, "version": {"number": version}}]})


def _written(page_id="999"):
    return _resp(
        {"id": page_id, "_links": {"base": "https://example.atlassian.net/wiki", "webui": f"/x/{page_id}"}}
    )


def test_missing_parent_is_created_then_page_published_as_child():
    with patch("connectors.docs.confluence.requests") as http:
        http.get.side_effect = [_empty_search(), _empty_search()]  # parent lookup, page lookup
        http.put.side_effect = AssertionError("no update expected")
        http.post.side_effect = [_written("100"), _written("200")]  # parent create, child create

        url = ConfluenceDocsConnector().publish_page(
            title="Reconciliation update - dev - x",
            content="line one\nline two",
            parent_title="Reconciliation Updates",
        )

        assert http.post.call_count == 2
        parent_payload = http.post.call_args_list[0].kwargs["json"]
        child_payload = http.post.call_args_list[1].kwargs["json"]
        assert parent_payload["title"] == "Reconciliation Updates"
        assert "ancestors" not in parent_payload
        assert child_payload["ancestors"] == [{"id": "100"}]
        assert child_payload["space"]["key"] == "SD"
        assert url == "https://example.atlassian.net/wiki/x/200"


def test_existing_parent_is_reused_not_recreated():
    with patch("connectors.docs.confluence.requests") as http:
        http.get.side_effect = [_found("100"), _empty_search()]
        http.put.side_effect = AssertionError("no update expected")
        http.post.side_effect = [_written("201")]

        ConfluenceDocsConnector().publish_page(
            title="dbt model: prep_sales_orders",
            content="<p>x</p>",
            parent_title="Model Documentation",
            content_format="storage",
        )

        assert http.post.call_count == 1  # only the child, parent reused
        assert http.post.call_args_list[0].kwargs["json"]["ancestors"] == [{"id": "100"}]


def test_existing_page_is_updated_with_incremented_version():
    with patch("connectors.docs.confluence.requests") as http:
        http.get.side_effect = [_found("100"), _found("200", version=3)]
        http.post.side_effect = AssertionError("no create expected")
        http.put.side_effect = [_written("200")]

        ConfluenceDocsConnector().publish_page(
            title="Serve Layer Overview",
            content="<table></table>",
            parent_title="Model Documentation",
            content_format="storage",
        )

        assert http.put.call_count == 1
        put_payload = http.put.call_args_list[0].kwargs["json"]
        assert put_payload["version"]["number"] == 4
        assert put_payload["id"] == "200"


def test_storage_format_is_published_verbatim_text_is_wrapped():
    with patch("connectors.docs.confluence.requests") as http:
        http.get.side_effect = [_found("100"), _empty_search(), _found("100"), _empty_search()]
        http.put.side_effect = AssertionError("no update expected")
        http.post.side_effect = [_written("1"), _written("2")]

        conn = ConfluenceDocsConnector()
        conn.publish_page(title="A", content="<h2>raw</h2>", parent_title="P", content_format="storage")
        conn.publish_page(title="B", content="plain text", parent_title="P", content_format="text")

        assert http.post.call_args_list[0].kwargs["json"]["body"]["storage"]["value"] == "<h2>raw</h2>"
        assert http.post.call_args_list[1].kwargs["json"]["body"]["storage"]["value"] == "<p>plain text</p>"


def test_no_parent_title_publishes_flat():
    with patch("connectors.docs.confluence.requests") as http:
        http.get.side_effect = [_empty_search()]  # only the page lookup, no parent lookup
        http.put.side_effect = AssertionError("no update expected")
        http.post.side_effect = [_written("5")]

        ConfluenceDocsConnector().publish_page(title="Flat", content="x", parent_title=None)

        assert "ancestors" not in http.post.call_args_list[0].kwargs["json"]
