"""Live Telegram smoke. Opt-in via env vars."""

from __future__ import annotations

import os
import pytest


pytestmark = pytest.mark.skipif(
    not all(os.environ.get(v) for v in (
        "MATRIX_TELEGRAM_BOT_TOKEN",
        "MATRIX_TELEGRAM_TEST_CHAT_ID",
    )),
    reason="set MATRIX_TELEGRAM_BOT_TOKEN + MATRIX_TELEGRAM_TEST_CHAT_ID",
)


@pytest.mark.asyncio
async def test_post_and_reply_via_reply_ui():
    pytest.skip(
        "Live Telegram journey scheduled to land alongside the "
        "portable stub harness; manual smoke verifies per spec §12."
    )
