"""E2E: a required-type policy parks the session; respond approves and the
tool actually fires; a separate run with rejected returns an error result.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_required_gate_full_cycle() -> None:
    pytest.skip(
        "E2E stub-LLM harness not yet portable; covered by unit + integration tests"
    )
