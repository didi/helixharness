"""A tiny task list used as the starting point for the HelixHarness walkthrough."""


def list_tasks(tasks):
    """Return task titles in their original order."""
    return [task["title"] for task in tasks]
