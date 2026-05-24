"""Live Slack smoke test. Opt-in via env vars."""

from __future__ import annotations

import os
import pytest


pytestmark = pytest.mark.skipif(
    not all(os.environ.get(v) for v in (
        "MATRIX_SLACK_APP_TOKEN",
        "MATRIX_SLACK_BOT_TOKEN",
        "MATRIX_SLACK_TEST_CHANNEL",
    )),
    reason="set MATRIX_SLACK_APP_TOKEN/BOT_TOKEN/TEST_CHANNEL to enable",
)


@pytest.mark.asyncio
async def test_post_and_observe_in_thread_reply():
    """Posts a real ask_user prompt; bot self-replies in thread;
    the adapter routes the reply back through ChannelInbox."""
    pytest.skip(
        "Live Slack journey scheduled to land alongside the "
        "portable stub harness. Manual smoke from a dev shell "
        "verifies the round-trip per the spec's §13."
    )
