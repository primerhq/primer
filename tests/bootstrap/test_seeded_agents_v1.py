"""The v1 freeze: the seeded definitions as they shipped before the core kit.

``SEEDED_AGENTS_V1`` is a verbatim copy of the definitions current main
shipped. Migration 11 upgrades a stored row only while it still equals
one of them, so the copy must never drift: a reworded description, a
reordered tool list or a new prompt line changes the digest and fails
this test.
"""
from __future__ import annotations

from primer.bootstrap.seeded_agents_v1 import (
    SEEDED_AGENTS_V1,
    definition_digest,
)


def test_v1_is_the_previous_shipped_definition():
    assert {k: definition_digest(v) for k, v in SEEDED_AGENTS_V1.items()} == {
        "operator": "392a1ac405e790ffed8eee8f2a077743c7ad81d6ac41ff4b12833843c59969c4",
        "builder": "77350429bf2f31f66d996900ce80896abe2b3ebb24ff47132953e23503ea961d",
        "planner": "6a659aaea802d72bb5e29a801878aa8ad7093d0a3514d92ddcbcf786647774ed",
        "explorer": "74aab5d24209667846fc3eb693e218287485bd114c40afd34e07c9bf63246b7a",
        "tool-runner": "f022c9e654fa0cdff8256d39c8893ee09a71f77377fa52521110e7fc3a9e4c79",
    }
