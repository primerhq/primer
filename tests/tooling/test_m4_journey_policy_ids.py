"""The M4 e2e journey's constants must match the live surfaces they drive (the journey is CI-only, so the unit lane pins them).

* The policy ids are the canonical seeded ids, never hand-rolled: a drifted spelling 404s against the shipped row.
* The MCP client timeout is an ``httpx2.Timeout``: ``create_mcp_http_client`` builds an ``httpx2.AsyncClient``, and an
  ``httpx.Timeout`` is a different package's class that httpcore2 cannot compose (job 131972235488 TypeError).
* The synchronous ``_call_output`` helper is never itself awaited: a double await raises TypeError only in the live journey
  (job 131991208076), so the unit lane scans the journey source for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx2

import tests.e2e.test_tools_universal_m4_mcp_journey as m4_journey
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


def test_m4_journey_never_awaits_the_sync_call_output_helper() -> None:
    # ``_call_output`` is synchronous; awaiting it (``await _call_output(...)``) raises TypeError only
    # in the live CI journey - a double await cost a 27-minute live run (job 131991208076). The unit
    # lane cannot run the journey, so scan its source: no Await may wrap a direct _call_output call.
    tree = ast.parse(Path(m4_journey.__file__).read_text())
    double_awaits = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_call_output"
    ]
    assert not double_awaits, double_awaits
