"""The tests/llm network guard (conftest.py) actually blocks and reports.

Written to behave identically with and without real networking (the lead
verifies tests/llm under ``unshare -rn``): no test depends on DNS, a live
resolver, or a usable loopback interface.
"""

from __future__ import annotations

import socket

import httpx
import pytest


def test_connect_to_a_remote_host_is_blocked_and_recorded(no_real_network) -> None:
    # Matched by type+message rather than importing NetworkBlocked: pytest
    # loads conftest.py under its own module name, so an import from here
    # would be a different class object.
    with pytest.raises(OSError, match="a mock missed"):
        socket.create_connection(("openrouter.ai", 443), timeout=1)
    assert "openrouter.ai" in no_real_network
    no_real_network.clear()  # expected here; keep teardown from failing the test


async def test_an_httpx_request_is_blocked_before_it_leaves(no_real_network) -> None:
    """The shape of the original bug: an unmocked request through a real
    client. Without the guard this sends a real request (and, against
    openrouter.ai, gets a real 401). A TEST-NET-3 literal keeps DNS out of
    it, so the connect guard is what has to catch it."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPError):
            await client.get("http://203.0.113.1/v1/models")
    assert "203.0.113.1" in no_real_network
    no_real_network.clear()


def test_loopback_passes_through_to_the_real_connect(no_real_network) -> None:
    """Loopback is not intercepted: the real connect runs and fails on its
    own terms (refused when lo is up, unreachable when it is not), which is
    never the guard's "a mock missed"."""
    with pytest.raises(OSError) as excinfo:
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    assert "a mock missed" not in str(excinfo.value)
    assert no_real_network == []
