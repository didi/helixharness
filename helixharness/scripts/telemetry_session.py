"""Optional host session-source lifecycle and a controlled identity failure."""


class SessionContextUnavailable(ValueError):
    """The host cannot bind this invocation to one real session and repository."""
