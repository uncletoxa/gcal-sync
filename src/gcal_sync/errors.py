class SyncTokenExpiredError(Exception):
    """Raised when a stored Google Calendar sync token is no longer valid (HTTP 410)."""


class AuthenticationError(Exception):
    """Raised when stored OAuth credentials are missing, invalid, or fail to refresh."""
