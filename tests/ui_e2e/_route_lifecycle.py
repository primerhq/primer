"""Drain owned page routes while their request context is still alive."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route


@contextmanager
def draining_page_routes(page: "Page") -> Iterator[list["Route"]]:
    """Own pass-through callbacks and any deliberately deferred requests.

    Tests append held routes to the yielded list and remove them when answered.
    On failure, outstanding held requests are aborted before waiting, so their
    unresolved handlers cannot deadlock cleanup. Callback failures propagate;
    this does not install Playwright's ignoreErrors behavior.
    """
    pending: list[Route] = []
    try:
        yield pending
    finally:
        # All aborts run even if one fails, and the drain always runs last.
        # ExitStack propagates failures rather than suppressing them.
        with ExitStack() as cleanup:
            cleanup.callback(page.unroute_all, behavior="wait")
            for route in pending:
                cleanup.callback(route.abort)
