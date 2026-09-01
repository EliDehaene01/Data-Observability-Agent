"""DocsConnector interface -- the abstraction every documentation-publishing
integration (Confluence today, others later) implements. Callers only ever
talk to this interface, never to a specific docs platform's API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class DocsConnector(ABC):
    @abstractmethod
    def publish_page(
        self,
        title: str,
        content: str,
        space_key: Optional[str] = None,
        parent_title: Optional[str] = None,
        content_format: str = "text",
    ) -> str:
        """Publish (create or update) a page with the given title/content,
        returning its URL.

        parent_title: optional title of a parent/section page the new page
        should be nested under. Implementations that support a page tree
        create the parent once if missing and reuse it thereafter; flat
        implementations may ignore it.

        content_format: "text" (default) -- `content` is plain text the
        implementation is free to format -- or "storage" -- `content` is
        already the target platform's native markup (Confluence storage
        format / HTML) and should be published verbatim.
        """
        raise NotImplementedError
