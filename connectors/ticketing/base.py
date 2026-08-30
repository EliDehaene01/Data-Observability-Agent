"""TicketConnector interface -- the abstraction every issue-tracker
integration (Jira today, others later) implements. Callers only ever talk
to this interface, never to a specific tracker's API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TicketConnector(ABC):
    @abstractmethod
    def create_ticket(self, summary: str, description: str, issue_type: str = "Bug") -> tuple[str, str]:
        """Create a ticket, returning (ticket_id, url)."""
        raise NotImplementedError
