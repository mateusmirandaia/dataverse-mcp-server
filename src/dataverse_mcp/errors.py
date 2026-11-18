"""Exception types surfaced to MCP clients.

Every message here may end up in front of a language model, so they are
written to be actionable: what went wrong and what to try next.
"""

from __future__ import annotations


class DataverseMCPError(Exception):
    """Base class for errors that are safe to show to the agent."""


class InvalidQueryError(DataverseMCPError, ValueError):
    """The agent asked for something we refuse to send to Dataverse."""


class WritesDisabledError(DataverseMCPError):
    def __init__(self) -> None:
        super().__init__(
            "Write operations are disabled on this server. "
            "Set DATAVERSE_ALLOW_WRITES=true to enable create_record and update_record."
        )


class AuthenticationError(DataverseMCPError):
    """Entra ID refused to issue a token."""


class DataverseError(DataverseMCPError):
    """A non-success response from the Dataverse Web API."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
