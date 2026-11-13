"""The M4 e2e journey's constants must match the live surfaces they drive (the journey is CI-only, so the unit lane pins them).

* The policy ids are the canonical seeded ids, never hand-rolled: a drifted spelling 404s against the shipped row.
* The MCP client timeout is an ``httpx2.Timeout``: ``create_mcp_http_client`` builds an ``httpx2.AsyncClient``, and an
  ``httpx.Timeout`` is a different package's class that httpcore2 cannot compose (job 131972235488 TypeError).
"""

from __future__ import annotations

import httpx2

from primer.bootstrap.seed import default_policy_id
from tests.e2e.test_tools_universal_m4_mcp_journey import _MCP_TIMEOUT, _POLICY_IDS

_TOOLS = ("put_config", "delete_config", "bootstrap")


def test_m4_journey_policy_ids_are_the_canonical_seeded_ids() -> None:
    assert set(_POLICY_IDS) == set(_TOOLS), _POLICY_IDS
    for tool in _TOOLS:
        assert _POLICY_IDS[tool] == default_policy_id("internal_collections", tool)


def test_m4_journey_mcp_timeout_is_httpx2() -> None:
    # The read bound carries the slow bootstrap call; 120s stays under the 180s CI step budget.
    assert isinstance(_MCP_TIMEOUT, httpx2.Timeout), type(_MCP_TIMEOUT)
    assert _MCP_TIMEOUT.read == 120.0
