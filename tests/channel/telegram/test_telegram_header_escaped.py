"""W39: the attribution header of a Telegram HTML post is escaped; a plain-text inform keeps it as is."""

from __future__ import annotations

import pytest

from tests.channel.test_outbound_posts_characterization import _env, telegram


@pytest.mark.parametrize("kind", ["ask_user", "tool_approval"])
async def test_html_posts_escape_the_header(kind: str) -> None:
    a, bot = telegram()
    await a.post_prompt(_env(kind, workspace_name="api <staging>", session_label="a&b", tool_name="t", tool_args={}))
    text = bot.sent[-1]["text"]
    assert text.startswith("\U0001F6E0 Workspace: api &lt;staging&gt; · Session: a&amp;b\n")
    assert bot.sent[-1]["parse_mode"] == "HTML"


async def test_plain_inform_keeps_the_header() -> None:
    a, bot = telegram()
    await a.post_prompt(_env("inform", workspace_name="api <staging>"))
    assert bot.sent[-1] == {"chat_id": "555", "text": "\U0001F6E0 Workspace: api <staging> · Session: s-1\nDone: all good"}
