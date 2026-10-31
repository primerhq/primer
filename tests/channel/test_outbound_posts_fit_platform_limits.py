"""W5: outbound posts fit each platform's limit; a long inform arrives as several posts, in order, in one thread.

Each fake sender refuses a post over the platform's documented limit, as the platform does (Discord 2000
characters of content, Telegram 4096 UTF-16 units of text, Slack 40000 characters of text and 3000 per
section block).
"""

from __future__ import annotations

import json

import pytest

from primer.channel.adapter import split_text
from primer.channel.telegram.render import build_tool_approval_message
from tests.channel.test_outbound_posts_characterization import _env, discord, slack, telegram


def _utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _long(n: int) -> str:
    words = [f"w{i:05d}" for i in range(n // 7 + 1)]
    lines = [" ".join(words[i:i + 12]) for i in range(0, len(words), 12)]
    return "\n".join(lines)[:n]


def _rejoined(parts: list[str], original: str) -> None:
    # Cut at a newline or space, which is dropped; never lose or reorder a character.
    assert "".join(parts).replace("\n", "").replace(" ", "") == original.replace("\n", "").replace(" ", "")


async def test_discord_long_inform_is_several_posts_in_the_thread() -> None:
    a, client = discord()
    text = _long(5000)
    await a.post_prompt(_env("inform", prompt=text, workspace_name=None, session_label=None))
    posts = client.log[1:]
    assert len(posts) == 3
    assert {tid for tid, _, _ in posts} == {501}
    assert all(len(c) <= 2000 for _, c, _ in posts)
    _rejoined([c for _, c, _ in posts], text)


async def test_telegram_long_inform_counts_utf16() -> None:
    a, bot = telegram()
    text = "\U0001F600" * 2500 + " " + "y" * 3000   # 5000 + 1 + 3000 UTF-16 units
    await a.post_prompt(_env("inform", prompt=text, workspace_name=None, session_label=None))
    assert len(bot.sent) == 2
    assert all(_utf16(m["text"]) <= 4096 and m["chat_id"] == "555" for m in bot.sent)
    _rejoined([m["text"] for m in bot.sent], text)


async def test_slack_long_inform_stays_in_the_session_thread() -> None:
    a, client = slack()
    text = _long(90_000)
    await a.post_prompt(_env("inform", prompt=text, workspace_name=None, session_label=None))
    posts = client.posts[1:]
    assert len(posts) == 3
    assert {p["thread_ts"] for p in posts} == {"t-1"}
    assert all(len(p["text"]) <= 40_000 for p in posts)
    _rejoined([p["text"] for p in posts], text)


async def test_slack_long_ask_user_prompt_is_several_sections() -> None:
    a, client = slack()
    text = _long(7000)
    await a.post_prompt(_env("ask_user", prompt=text))
    blocks = client.posts[-1]["blocks"]
    sections = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert len(sections) == 3 and all(len(s) <= 3000 for s in sections)
    _rejoined(sections, text)
    assert blocks[-1]["type"] == "context"


@pytest.mark.parametrize("content", ["x" * 10_000, "\U0001F600" * 10_000])
def test_telegram_approval_text_is_bounded(content: str) -> None:
    env = _env("tool_approval", prompt="p", tool_name="write_file", tool_args={"path": "a.py", "content": content})
    body = build_tool_approval_message(chat_id="1", envelope=env)
    assert _utf16(body["text"]) <= 4096
    assert body["text"].endswith("\n... (truncated)</pre>")
    pretty = json.dumps(env.tool_args, indent=2, ensure_ascii=False)
    assert pretty[:200] in body["text"]


def test_split_text_keeps_short_text_whole() -> None:
    assert split_text("abc", 3) == ["abc"]
    assert split_text("", 3) == [""]
    assert split_text("ab cd", 2) == ["ab", "cd"]
    assert split_text("abcdef", 4) == ["abcd", "ef"]
    assert split_text("\U0001F600\U0001F600x", 3) == ["\U0001F600", "\U0001F600x"]
