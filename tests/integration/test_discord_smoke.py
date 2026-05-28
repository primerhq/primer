"""Live Discord smoke. Opt-in via env vars."""

from __future__ import annotations

import os
import pytest


pytestmark = pytest.mark.skipif(
    not all(os.environ.get(v) for v in (
        "PRIMER_DISCORD_BOT_TOKEN",
        "PRIMER_DISCORD_TEST_CHANNEL_ID",
    )),
    reason="set PRIMER_DISCORD_BOT_TOKEN + PRIMER_DISCORD_TEST_CHANNEL_ID",
)


@pytest.mark.asyncio
async def test_post_approval_and_thread_reply():
    pytest.skip(
        "Live Discord journey scheduled to land alongside the "
        "portable stub harness; manual smoke verifies per spec §12."
    )
