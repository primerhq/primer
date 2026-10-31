"""Characterization: what each channel adapter posts today for posts under the platform limits.

Pins the calls (text, thread, order), the return values, the Slack and Discord args truncation, the
ask_user thread rows and the absence of a row on a shared anchor, so Task 74 (W5, W35, W39, W56j) can be
shown to change nothing a post under the limit already gets.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.channel.adapter import PromptEnvelope, format_tool_args
from primer.channel.correlation import CorrelationStore
from primer.channel.discord.adapter import DiscordChannelAdapter
from primer.channel.slack.adapter import SlackChannelAdapter
from primer.channel.telegram.adapter import TelegramChannelAdapter
from tests.conftest import _FakeStorageProvider

HEADER = "\U0001F6E0 Workspace: Ops · Session: s-1\n"
BIG_ARGS = {"path": "a.py", "content": "x" * 10_000}


def _env(kind: str, prompt: str = "Done: all good", **extra) -> PromptEnvelope:
    base = dict(
        kind=kind, workspace_id="ws-1", session_id="s1", tool_call_id="tc-1" if kind != "inform" else "",
        prompt=prompt, response_schema=None, choices=None, timeout_at_iso=None,
        workspace_name="Ops", session_label="s-1",
    )
    base.update(extra)
    return PromptEnvelope(**base)


# ---- fakes ----------------------------------------------------------------------------------------------------


class SlackClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"t-{len(self.posts)}", "channel": kw["channel"]}


def slack(sp=None) -> tuple[SlackChannelAdapter, SlackClient]:
    a = SlackChannelAdapter(
        provider=SimpleNamespace(id="cp"), channel=SimpleNamespace(id="ch-1", external_id="C123"),
        inbox=None, storage_provider=sp,
    )
    client = SlackClient()
    a._conn = SimpleNamespace(app=SimpleNamespace(client=client))
    return a, client


class DiscordThread:
    def __init__(self, tid: int, log: list) -> None:
        self.id = tid
        self._log = log

    async def send(self, *, content: str, view=None):
        self._log.append((self.id, content, view is not None))
        return SimpleNamespace(id=1000 + len(self._log))


class DiscordAnchor:
    def __init__(self, mid: int, log: list, client) -> None:
        self.id = mid
        self._log = log
        self._client = client

    async def create_thread(self, *, name: str, auto_archive_duration: int):
        thread = DiscordThread(self.id, self._log)
        self._client.channels[thread.id] = thread
        return thread


class DiscordChannel:
    def __init__(self, cid: int, log: list, client) -> None:
        self.id = cid
        self._log = log
        self._client = client

    async def send(self, *, content: str):
        self._log.append((self.id, content, False))
        return DiscordAnchor(500 + len(self._log), self._log, self._client)


class DiscordClient:
    def __init__(self) -> None:
        self.log: list[tuple[int, str, bool]] = []
        self.channels: dict[int, object] = {}
        self.channels[9001] = DiscordChannel(9001, self.log, self)

    def get_channel(self, cid: int):
        return self.channels.get(cid)

    async def fetch_channel(self, cid: int):
        return self.channels.get(cid)


def discord(sp=None) -> tuple[DiscordChannelAdapter, DiscordClient]:
    a = DiscordChannelAdapter(
        provider=SimpleNamespace(id="cp"), channel=SimpleNamespace(id="ch-1", external_id="9001"),
        inbox=None, storage_provider=sp,
    )
    client = DiscordClient()
    a._client = client
    return a, client


class TelegramBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=len(self.sent))


def telegram(sp=None) -> tuple[TelegramChannelAdapter, TelegramBot]:
    a = TelegramChannelAdapter(
        provider=SimpleNamespace(id="cp"), channel=SimpleNamespace(id="ch-1", external_id="555"),
        inbox=None, storage_provider=sp,
    )
    bot = TelegramBot()
    a._app = SimpleNamespace(bot=bot)
    return a, bot


# ---- inform under the limit -----------------------------------------------------------------------------------


async def test_slack_inform_under_the_limit() -> None:
    a, client = slack()
    out = await a.post_prompt(_env("inform"))
    assert client.posts == [
        {"channel": "C123", "text": ":thread: Agent session s1"},
        {"channel": "C123", "thread_ts": "t-1", "text": HEADER + "Done: all good"},
    ]
    assert out == {"ts": "", "channel": "C123", "thread_ts": "t-1"}


async def test_slack_inform_at_exactly_the_text_limit_is_one_post() -> None:
    a, client = slack()
    text = "y" * (40_000 - len(HEADER))
    await a.post_prompt(_env("inform", prompt=text, thread_anchor="t-0"))
    assert client.posts == [{"channel": "C123", "thread_ts": "t-0", "text": HEADER + text}]


async def test_discord_inform_under_the_limit() -> None:
    a, client = discord()
    out = await a.post_prompt(_env("inform"))
    assert client.log == [(9001, ":thread: Agent session s1", False), (501, HEADER + "Done: all good", False)]
    assert out == {"message_id": 1002, "thread_id": 501}


async def test_discord_inform_at_exactly_2000_is_one_post() -> None:
    a, client = discord()
    text = "y" * (2000 - len(HEADER))
    await a.post_prompt(_env("inform", prompt=text))
    assert [c for _, c, _ in client.log[1:]] == [HEADER + text]


async def test_telegram_inform_under_the_limit() -> None:
    a, bot = telegram()
    out = await a.post_prompt(_env("inform"))
    assert bot.sent == [{"chat_id": "555", "text": HEADER + "Done: all good"}]
    assert out == {"message_id": 1}


async def test_telegram_inform_at_exactly_4096_is_one_post() -> None:
    a, bot = telegram()
    text = "y" * (4096 - len(HEADER.encode("utf-16-le")) // 2)
    await a.post_prompt(_env("inform", prompt=text))
    assert bot.sent == [{"chat_id": "555", "text": HEADER + text}]


# ---- approvals and asks ---------------------------------------------------------------------------------------


async def test_slack_approval_args_truncation() -> None:
    a, client = slack()
    await a.post_prompt(_env("tool_approval", prompt="p", tool_name="write_file", tool_args=BIG_ARGS))
    blocks = client.posts[-1]["blocks"]
    args = format_tool_args(BIG_ARGS)[:2800] + "\n... (truncated)"
    assert blocks[2] == {"type": "section", "text": {"type": "mrkdwn", "text": f"*Arguments:*\n```{args}```"}}
    assert client.posts[-1]["text"] == HEADER + "p"


async def test_slack_ask_user_short_prompt_blocks() -> None:
    a, client = slack()
    await a.post_prompt(_env("ask_user", prompt="Which branch?"))
    assert client.posts[-1]["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Which branch?"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "_Reply in this thread to answer._"}]},
    ]
    assert client.posts[-1]["text"] == HEADER + "Which branch?"


async def test_discord_approval_args_truncation() -> None:
    a, client = discord()
    await a.post_prompt(_env("tool_approval", prompt="p", tool_name="write_file", tool_args=BIG_ARGS))
    args = format_tool_args(BIG_ARGS)[:1700] + "\n... (truncated)"
    content = client.log[-1][1]
    assert content == HEADER + (
        ":lock: **Tool approval requested**\n**Tool:** `write_file`\n" f"**Arguments:**\n```json\n{args}\n```"
    )
    assert client.log[-1][2] is True


async def test_telegram_approval_short_args() -> None:
    a, bot = telegram()
    await a.post_prompt(_env("tool_approval", prompt="p", tool_name="write_file", tool_args={"path": "a.py"}))
    body = bot.sent[-1]
    assert body["text"] == HEADER + (
        "\U0001F7E1 <b>Primer · approval needed</b>\n\nRun tool <b>write_file</b>\n"
        '<pre>{\n  "path": "a.py"\n}</pre>'
    )
    assert body["parse_mode"] == "HTML"
    assert [b["text"] for b in body["reply_markup"]["inline_keyboard"][0]] == ["✅ Approve", "❌ Reject"]


async def test_telegram_ask_user_plain_header() -> None:
    a, bot = telegram()
    await a.post_prompt(_env("ask_user", prompt="Which <branch>?"))
    assert bot.sent[-1]["text"] == HEADER + (
        "\U0001F535 <b>Primer · input needed</b>\n\nWhich &lt;branch&gt;?\n\n"
        "<i>↳ Reply to this message to answer.</i>"
    )


# ---- correlation rows -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("make", "anchor"), [(slack, "t-1"), (discord, "501")], ids=["slack", "discord"])
async def test_ask_user_writes_its_thread_row(make, anchor: str) -> None:
    sp = _FakeStorageProvider()
    a, _ = make(sp)
    await a.post_prompt(_env("ask_user", prompt="Which branch?"))
    row = await CorrelationStore(sp).lookup("ch-1", anchor)
    assert (row.workspace_id, row.session_id, row.tool_call_id) == ("ws-1", "s1", "tc-1")


@pytest.mark.parametrize(("make", "anchor"), [(slack, "t-shared"), (discord, "777")], ids=["slack", "discord"])
@pytest.mark.parametrize("kind", ["inform", "tool_approval"])
async def test_a_shared_anchor_gets_no_row(make, anchor: str, kind: str) -> None:
    sp = _FakeStorageProvider()
    a, client = make(sp)
    if make is discord:
        client.channels[777] = DiscordThread(777, client.log)
    await a.post_prompt(_env(kind, thread_anchor=anchor, tool_name="t", tool_args={}))
    assert await CorrelationStore(sp).list_for_channel("ch-1") == []
