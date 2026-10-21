"""HelixHarness Platform Web adapter (FastAPI + HTMX + Tailwind)."""


def create_app():
    """Build the Web application without importing the server on package load."""

    from web_ui.main import create_app as build_app

    return build_app()

__all__ = ["create_app"]
