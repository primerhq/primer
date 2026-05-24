"""E2E: NullAdapter full round-trip journey.

Skipped until the portable stub-LLM harness lands; unit tests in
tests/channel/* + tests/worker/test_post_park_channel_dispatch.py
cover the dispatch + resume paths comprehensively.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_null_adapter_full_journey() -> None:
    pytest.skip(
        "E2E NullAdapter journey scheduled to land alongside the "
        "portable stub-LLM harness; unit tests cover the dispatch "
        "+ resume paths comprehensively."
    )
