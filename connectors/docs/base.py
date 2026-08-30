"""DocsConnector interface -- the abstraction every documentation-publishing
integration (Confluence today, others later) implements. Callers only ever
talk to this interface, never to a specific docs platform's API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class DocsConnector(ABC):
    @abstractmethod
    def publish_page(self, title: str, content: str, space_key: Optional[str] = None) -> str:
        """Publish a page with the given title/content, returning its URL."""
        raise NotImplementedError
