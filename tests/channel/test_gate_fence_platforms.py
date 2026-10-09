"""Each platform keeps the gate id with the button or the message it posted, and brings it back with the click (console review C-033).

* Telegram: ``callback_data`` is capped at 64 bytes, so the id cannot ride in it. The tag is hashed over the gate id as well (one tag per gate, so a
  later prompt under the same tool_call_id no longer overwrites the earlier button's entry) and the full id lives in the tag's cache entry; a prompt
  with no gate id hashes exactly as before. The ask_user reply is correlated by the message it replies to, so the persistent row carries the id.
* Slack: the button value and the reject modal's metadata carry ``#<gate id>`` after the tool_call_id.
* Discord: the custom id carries ``#<first 12 characters>`` after the tool_call_id when that keeps it under the platform's 100-character limit,
  and is left as it was when it would not.
"""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from primer.channel.adapter import APPROVAL_STALE_NOTICE, QUESTION_STALE_NOTICE, DecisionRefused, PromptEnvelope
from primer.channel.correlation import CorrelationStore
from primer.channel.discord.views import ApprovalView, build_approval_custom_ids, decode_custom_id, decode_custom_id_with_gate
from primer.channel.slack import factory as slack_factory
from primer.channel.slack.render import build_reject_modal, build_tool_approval_message
from primer.channel.telegram.adapter import TelegramChannelAdapter
from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS
from primer.channel.telegram.render import build_tool_approval_message as tg_approval_message
from primer.channel.telegram.render import compute_tag
from primer.model.channel import Channel, ChannelProvider, ChannelProviderType, TelegramChannelProviderConfig
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests.channel.slack.test_factory import _FakeEntry as _SlackEntry
from tests.channel.slack.test_factory import _install as _slack_install
from tests.channel.slack.test_factory import _mock_adapter as _slack_adapter
from tests.channel.telegram import test_factory as tg_tests

G1 = "a" * 32
G2 = "b" * 32


def _approval(gate_id: str | None, *, tool_call_id: str = "tc-1") -> PromptEnvelope:
    return PromptEnvelope(
        kind="tool_approval", workspace_id="ws", session_id="s", tool_call_id=tool_call_id, prompt="Approve write?", response_schema=None,
        choices=["Approve", "Reject"], timeout_at_iso=None, tool_name="write", tool_args={"path": "a"}, gate_id=gate_id,
    )


# ---- Telegram --------------------------------------------------------------------------------------------------------------------------------


def test_the_telegram_tag_is_per_gate_and_unchanged_without_one() -> None:
    legacy = base64.urlsafe_b64encode(hashlib.sha256(b"ws|s|tc-1").digest()[:12]).rstrip(b"=").decode("ascii")

    assert compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc-1") == legacy
    first = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc-1", gate_id=G1)
    second = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc-1", gate_id=G2)
    assert len({legacy, first, second}) == 3
    assert len("a:" + first) <= 64


def test_the_approval_buttons_carry_the_gate_scoped_tag() -> None:
    body = tg_approval_message(chat_id="1", envelope=_approval(G1))

    tag = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc-1", gate_id=G1)
    assert [b["callback_data"] for b in body["reply_markup"]["inline_keyboard"][0]] == [f"a:{tag}", f"r:{tag}"]


class _Bot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **body):
        self.sent.append(body)
        return SimpleNamespace(message_id=40 + len(self.sent))


def _tg_adapter(monkeypatch):
    app = SimpleNamespace(bot=_Bot())

    async def _acquire(_):
        return app

    async def _release(_):
        return None

    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "acquire", _acquire)
    monkeypatch.setattr(TELEGRAM_CONNECTIONS, "release", _release)
    provider = ChannelProvider(
        id="cp-1", provider=ChannelProviderType.TELEGRAM,
        config=TelegramChannelProviderConfig(bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456")),
    )
    channel = Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="123456789")
    return TelegramChannelAdapter(provider=provider, channel=channel, inbox=SimpleNamespace()), app


@pytest.mark.asyncio
async def test_two_prompts_under_one_tool_call_id_keep_their_own_gate_id(monkeypatch) -> None:
    """Round 3's prompt used to overwrite round 1's tag entry, so round 1's button resolved to the NEW call."""
    adapter, app = _tg_adapter(monkeypatch)
    await adapter.initialize()
    try:
        await adapter.post_prompt(_approval(G1))
        await adapter.post_prompt(_approval(G2))
    finally:
        await adapter.aclose()

    first = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc-1", gate_id=G1)
    second = compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc-1", gate_id=G2)
    assert (await adapter._resolve_tag(first))["gate_id"] == G1
    assert (await adapter._resolve_tag(second))["gate_id"] == G2
    assert f"a:{first}" in str(app.bot.sent[0]["reply_markup"]) and f"a:{second}" in str(app.bot.sent[1]["reply_markup"])


@pytest.mark.asyncio
async def test_a_prompt_without_a_gate_id_caches_the_same_ids_as_before(monkeypatch) -> None:
    adapter, _ = _tg_adapter(monkeypatch)
    await adapter.initialize()
    try:
        await adapter.post_prompt(_approval(None))
    finally:
        await adapter.aclose()

    ids = await adapter._resolve_tag(compute_tag(workspace_id="ws", session_id="s", tool_call_id="tc-1"))
    assert ids == {"workspace_id": "ws", "session_id": "s", "tool_call_id": "tc-1"}


@pytest.mark.asyncio
async def test_the_reject_reply_brings_the_gate_id_back(monkeypatch) -> None:
    """The Reject button asks for a reason in a reply; that reply decides the gate the button belonged to."""
    adapter = tg_tests._mock_adapter()
    target = {"kind": "reject", "workspace_id": "w", "session_id": "s", "tool_call_id": "t", "gate_id": G1}
    adapter.resolve_reply_target.return_value = target
    _, on_message = tg_tests._install(monkeypatch, tg_tests._FakeEntry({"100": adapter}))
    adapter._sp = None                      # no persistent row: the in-memory reject target answers
    msg = tg_tests._msg(text="too risky", reply_to=SimpleNamespace(message_id=33))

    await on_message(SimpleNamespace(message=msg), tg_tests._context())

    kw = adapter._handle_decision.await_args.kwargs
    assert kw["decision"] == "rejected" and kw["gate_id"] == G1


@pytest.mark.asyncio
async def test_the_ask_user_reply_brings_the_rows_gate_id_back(monkeypatch) -> None:
    adapter = tg_tests._mock_adapter()
    rec = SimpleNamespace(kind="session", workspace_id="w", session_id="s", tool_call_id="t", gate_id=G1)

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return rec

        async def clear(self, cid, key):
            return None

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    _, on_message = tg_tests._install(monkeypatch, tg_tests._FakeEntry({"100": adapter}))

    await on_message(SimpleNamespace(message=tg_tests._msg(text="EUR", reply_to=SimpleNamespace(message_id=33))), tg_tests._context())

    assert adapter._handle_text_reply.await_args.kwargs["gate_id"] == G1


@pytest.fixture
async def store(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "c.sqlite"))
    await sp.initialize()
    yield CorrelationStore(sp)
    await sp.aclose()


@pytest.mark.asyncio
async def test_the_correlation_row_keeps_the_gate_id_and_clearing_the_gate_clears_it(store) -> None:
    await store.upsert_session(channel_id="ch", anchor="m1", workspace_id="w", session_id="s", tool_call_id="tc", gate_id=G1)
    assert (await store.lookup("ch", "m1")).gate_id == G1

    await store.upsert_session(channel_id="ch", anchor="m1", workspace_id="w", session_id="s", tool_call_id="tc", gate_id=G2)
    assert (await store.lookup("ch", "m1")).gate_id == G2, "the next prompt on the same anchor replaces it"

    await store.clear_gate("ch", "m1")
    row = await store.lookup("ch", "m1")
    assert row.tool_call_id is None and row.gate_id is None

    await store.upsert_session(channel_id="ch", anchor="m2", workspace_id="w", session_id="s", tool_call_id="tc")
    assert (await store.lookup("ch", "m2")).gate_id is None


# ---- Slack -----------------------------------------------------------------------------------------------------------------------------------


def test_the_slack_buttons_carry_the_gate_id_after_the_tool_call_id() -> None:
    body = build_tool_approval_message(channel_id="C1", envelope=_approval(G1))

    buttons = next(b for b in body["blocks"] if b["type"] == "actions")["elements"]
    assert [b["value"] for b in buttons] == [f"approve:ws:s:tc-1#{G1}", f"reject:ws:s:tc-1#{G1}"]


def test_a_slack_button_without_a_gate_id_is_unchanged() -> None:
    body = build_tool_approval_message(channel_id="C1", envelope=_approval(None))

    buttons = next(b for b in body["blocks"] if b["type"] == "actions")["elements"]
    assert [b["value"] for b in buttons] == ["approve:ws:s:tc-1", "reject:ws:s:tc-1"]


def test_the_reject_modal_carries_the_gate_id_in_its_metadata() -> None:
    view = build_reject_modal(workspace_id="ws", session_id="s", tool_call_id="tc-1", gate_id=G1, channel_id="C1", message_ts="1.2")

    assert view["private_metadata"] == f"reject:ws:s:tc-1#{G1}:C1:1.2"


@pytest.mark.asyncio
async def test_a_slack_approve_click_hands_the_inbox_the_id_and_a_plain_tool_call_id(monkeypatch) -> None:
    adapter = _slack_adapter()
    _, app = _slack_install(monkeypatch, _SlackEntry({"C123": adapter}))
    body = {
        "actions": [{"value": f"approve:ws1:sid1:tc1#{G1}"}], "channel": {"id": "C123"}, "user": {"id": "U9"},
        "message": {"ts": "111.222", "blocks": []},
    }

    await app.actions["approve"](AsyncMock(), body, SimpleNamespace(chat_update=AsyncMock()))

    kw = adapter._handle_decision.await_args.kwargs
    assert kw["tool_call_id"] == "tc1" and kw["gate_id"] == G1


@pytest.mark.asyncio
async def test_a_slack_modal_submit_hands_the_inbox_the_id_too(monkeypatch) -> None:
    adapter = _slack_adapter()
    _, app = _slack_install(monkeypatch, _SlackEntry({"C123": adapter}))
    view = {"private_metadata": f"reject:ws:sid:tc#{G1}:C123:111.2", "state": {"values": {"reason": {"reason_text": {"value": "no"}}}}}
    client = SimpleNamespace(conversations_history=AsyncMock(return_value={"messages": [{}]}), chat_update=AsyncMock())

    await app.views[slack_factory.REJECT_MODAL_CALLBACK_ID](AsyncMock(), {"user": {"id": "U1"}}, view, client)

    kw = adapter._handle_decision.await_args.kwargs
    assert kw["tool_call_id"] == "tc" and kw["gate_id"] == G1


@pytest.mark.asyncio
async def test_a_slack_click_on_an_old_button_has_no_gate_id(monkeypatch) -> None:
    adapter = _slack_adapter()
    _, app = _slack_install(monkeypatch, _SlackEntry({"C123": adapter}))
    body = {"actions": [{"value": "approve:ws1:sid1:tc1"}], "channel": {"id": "C123"}, "user": {"id": "U9"}, "message": {"ts": "1", "blocks": []}}

    await app.actions["approve"](AsyncMock(), body, SimpleNamespace(chat_update=AsyncMock()))

    kw = adapter._handle_decision.await_args.kwargs
    assert kw["tool_call_id"] == "tc1" and kw["gate_id"] is None


# ---- Discord ---------------------------------------------------------------------------------------------------------------------------------


def test_the_discord_custom_ids_carry_a_short_gate_token_when_it_fits() -> None:
    approve, reject = build_approval_custom_ids(ws="ws", sid="s", tcid="tc-1", gate_id=G1)

    assert approve == f"approve:ws:s:tc-1#{G1[:12]}" and reject == f"reject:ws:s:tc-1#{G1[:12]}"
    assert decode_custom_id_with_gate(approve) == ("approve", "ws", "s", "tc-1", G1[:12])


def test_the_discord_custom_ids_keep_the_old_shape_without_a_gate_id() -> None:
    assert build_approval_custom_ids(ws="ws", sid="s", tcid="tc-1") == ("approve:ws:s:tc-1", "reject:ws:s:tc-1")
    assert decode_custom_id_with_gate("approve:ws:s:tc-1") == ("approve", "ws", "s", "tc-1", None)
    assert decode_custom_id("approve:ws:s:tc-1") == ("approve", "ws", "s", "tc-1")


def test_the_token_goes_on_only_if_the_longest_discord_id_still_fits_in_100_characters() -> None:
    """The reject modal's custom id is the longest of the three, so it decides for all of them (a token on Approve and none on Reject would be odd)."""
    from primer.channel.discord.views import REJECT_MODAL_CUSTOM_ID_PREFIX, build_reject_modal

    fits = "t" * 40            # modal id: 19 + 1 + 11 + 1 + 9 + 1 + 40 = 82, +13 for the token = 95
    too_long = "t" * 50        # 92 without the token, 105 with it

    approve, reject = build_approval_custom_ids(ws="workspace-1", sid="session-1", tcid=fits, gate_id=G1)
    assert approve.endswith("#" + G1[:12]) and reject.endswith("#" + G1[:12])
    modal = build_reject_modal(ws="workspace-1", sid="session-1", tcid=fits, gate_id=G1, on_submit=AsyncMock())
    assert modal.custom_id == f"{REJECT_MODAL_CUSTOM_ID_PREFIX}:workspace-1:session-1:{fits}#{G1[:12]}" and len(modal.custom_id) <= 100

    approve, reject = build_approval_custom_ids(ws="workspace-1", sid="session-1", tcid=too_long, gate_id=G1)
    assert "#" not in approve and "#" not in reject
    modal = build_reject_modal(ws="workspace-1", sid="session-1", tcid=too_long, gate_id=G1, on_submit=AsyncMock())
    assert "#" not in modal.custom_id and len(modal.custom_id) <= 100


def test_the_discord_view_buttons_use_those_custom_ids() -> None:
    view = ApprovalView(ws="ws", sid="s", tcid="tc-1", gate_id=G1)

    assert [c.custom_id for c in view.children] == [f"approve:ws:s:tc-1#{G1[:12]}", f"reject:ws:s:tc-1#{G1[:12]}"]


@pytest.mark.asyncio
async def test_a_discord_approve_click_hands_the_inbox_the_token_and_a_plain_tool_call_id(monkeypatch) -> None:
    from tests.channel.discord import test_factory as dc_tests

    adapter = dc_tests._mock_adapter()
    _, client = dc_tests._install(monkeypatch, dc_tests._FakeEntry({"100": adapter}))
    inter = dc_tests._interaction(custom_id=f"approve:w:s:t#{G1[:12]}", parent_id=None, channel_id=100)

    await client.on_interaction(inter)

    kw = adapter._handle_decision.await_args.kwargs
    assert kw["tool_call_id"] == "t" and kw["gate_id"] == G1[:12]


# ---- a refused click or reply is told so -------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_telegram_click_alerts_only_the_clicker_and_edits_nothing(monkeypatch) -> None:
    adapter = tg_tests._mock_adapter()
    adapter._resolve_tag = AsyncMock(return_value={"workspace_id": "w", "session_id": "s", "tool_call_id": "t", "gate_id": G1})
    adapter._handle_decision = AsyncMock(return_value=DecisionRefused(APPROVAL_STALE_NOTICE))
    on_callback, _ = tg_tests._install(monkeypatch, tg_tests._FakeEntry({"100": adapter}))
    ctx, cq = tg_tests._context(), tg_tests._cq("a:TAG")

    await on_callback(SimpleNamespace(callback_query=cq), ctx)

    assert adapter._handle_decision.await_args.kwargs["gate_id"] == G1
    cq.answer.assert_awaited_once_with(text=APPROVAL_STALE_NOTICE, show_alert=True)
    ctx.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_telegram_reply_to_a_replaced_question_is_told_so(monkeypatch) -> None:
    adapter = tg_tests._mock_adapter()
    adapter._handle_text_reply = AsyncMock(return_value=DecisionRefused(QUESTION_STALE_NOTICE))
    rec = SimpleNamespace(kind="session", workspace_id="w", session_id="s", tool_call_id="t", gate_id=G1)

    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return rec

        async def clear(self, cid, key):
            return None

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)
    _, on_message = tg_tests._install(monkeypatch, tg_tests._FakeEntry({"100": adapter}))
    ctx = tg_tests._context()

    await on_message(SimpleNamespace(message=tg_tests._msg(text="EUR", reply_to=SimpleNamespace(message_id=33))), ctx)

    assert ctx.bot.send_message.await_args.kwargs["text"] == QUESTION_STALE_NOTICE


@pytest.mark.asyncio
async def test_a_stale_slack_click_tells_only_the_clicker_and_leaves_the_message(monkeypatch) -> None:
    adapter = _slack_adapter()
    adapter._handle_decision = AsyncMock(return_value=DecisionRefused(APPROVAL_STALE_NOTICE))
    _, app = _slack_install(monkeypatch, _SlackEntry({"C123": adapter}))
    client = SimpleNamespace(chat_update=AsyncMock(), chat_postEphemeral=AsyncMock())
    body = {"actions": [{"value": f"approve:ws1:sid1:tc1#{G1}"}], "channel": {"id": "C123"}, "user": {"id": "U9"}, "message": {"ts": "1", "blocks": []}}

    await app.actions["approve"](AsyncMock(), body, client)

    client.chat_update.assert_not_awaited()
    kw = client.chat_postEphemeral.await_args.kwargs
    assert kw["channel"] == "C123" and kw["user"] == "U9" and kw["text"] == APPROVAL_STALE_NOTICE


@pytest.mark.asyncio
async def test_a_stale_discord_click_tells_only_the_clicker(monkeypatch) -> None:
    from tests.channel.discord import test_factory as dc_tests

    adapter = dc_tests._mock_adapter()
    adapter._handle_decision = AsyncMock(return_value=DecisionRefused(APPROVAL_STALE_NOTICE))
    _, client = dc_tests._install(monkeypatch, dc_tests._FakeEntry({"100": adapter}))
    inter = dc_tests._interaction(custom_id=f"approve:w:s:t#{G1[:12]}", channel_id=100)

    await client.on_interaction(inter)

    inter.edit_original_response.assert_not_awaited()
    inter.followup.send.assert_awaited_once()
    assert inter.followup.send.await_args.args[0] == APPROVAL_STALE_NOTICE
    assert inter.followup.send.await_args.kwargs["ephemeral"] is True
