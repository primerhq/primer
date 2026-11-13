"""The M4 e2e journey's policy ids must be the canonical seeded ids, never hand-rolled.

A hand-rolled ``tool-approval-policy-internal-collections-<tool>`` spelling drifts from
``primer.bootstrap.seed.default_policy_id`` (the toolset part of the id keeps its
underscore), and a journey GET on the drifted id 404s against the shipped row. This pin
keeps the journey constant locked to the canonical helper so the drift cannot recur
silently (the e2e journey itself is CI-owned and does not run in the unit sweep).
"""

from __future__ import annotations

from primer.bootstrap.seed import default_policy_id
from tests.e2e.test_tools_universal_m4_mcp_journey import _POLICY_IDS

_TOOLS = ("put_config", "delete_config", "bootstrap")


def test_m4_journey_policy_ids_are_the_canonical_seeded_ids() -> None:
    assert set(_POLICY_IDS) == set(_TOOLS), _POLICY_IDS
    for tool in _TOOLS:
        assert _POLICY_IDS[tool] == default_policy_id("internal_collections", tool)
