"""A toolset that cannot list its tools is left out of the turn (platform revamp, design 20 Q4).

Before: a remote (MCP) toolset whose listing failed, or never answered, ended the turn. Now the turn goes on without it,
each remote listing is bounded (``tool_listing.TURN_TOOLSET_LIST_TIMEOUT_S``), the model is told in the system message it is
sent (never persisted), and a call that names one of the left-out toolset's tools gets a tool error with the reason.

The core of the review is cancellation: a Stop or a session Cancel that lands while the listing waits is the caller's, and the
wait must never turn it into "this toolset is unavailable". Those tests are marked below.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest

# Importing primer.workspace makes ToolCallContext.model_rebuild() run, as in the other tool-manager tests.
import primer.workspace  # noqa: F401
from primer.agent import tool_listing
from primer.agent.interrupt import Interrupted, interruptible
from primer.agent.loop import run_agent_turn
from primer.agent.tool_listing import (
    NOTICE_HEADER,
    TURN_TOOLSET_LIST_TIMEOUT_S,
    ToolListing,
    list_remote_toolset,
    unavailable_toolsets_notice,
    with_system_notice,
)
from primer.agent.tool_manager import ToolExecutionManager
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Done, Message, TextPart, Tool, ToolCallPart, ToolCallResult
from primer.model.except_ import AuthRequiredError, ConfigError, NetworkError, ProviderError
from primer.model.principal import PrincipalRef
from primer.model.provider import HttpConfig, McpConfig, StdioConfig, TransportType
from primer.toolset.mcp import McpToolsetProvider
from tests.agent import test_subagent_stop as subagent_stop
from tests.agent.test_run_agent_turn_seam import AGENT, MODEL, _Manager, _RoundsLLM

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _tool(name: str, toolset_id: str) -> Tool:
    return Tool(id=name, description=f"the tool {name}", toolset_id=toolset_id, args_schema=_SCHEMA)


TOOL_A = _tool("a", "misc")
TOOL_SEARCH = _tool("search", "kb")


class _Local:
    """A provider that lists in-process: no ``lists_remotely`` at all (a duck-typed fake, like the other tool-manager tests)."""

    def __init__(self, tools: list[Tool] | None = None, *, raises: Exception | None = None) -> None:
        self.tools = list(tools or [])
        self.raises = raises
        self.listings = 0

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        self.listings += 1
        if self.raises is not None:
            raise self.raises
        for tool in self.tools:
            yield tool

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        return ToolCallResult(output=f"{tool_name}()", is_error=False)


class _Remote(_Local):
    """A provider that lists over the network: it raises, or hangs, or answers; it records that its listing began and was cancelled.

    ``teardown`` makes a cancelled listing slow to die: it holds the CancelledError until the event is set."""

    lists_remotely = True

    def __init__(
        self,
        tools: list[Tool] | None = None,
        *,
        raises: BaseException | None = None,
        hang: bool = False,
        teardown: asyncio.Event | None = None,
    ) -> None:
        super().__init__(tools, raises=None)
        self.remote_raises = raises
        self.hang = hang
        self.teardown = teardown
        self.listing_cancelled = False
        self.entered = asyncio.Event()  # the listing has begun
        self.cancel_seen = asyncio.Event()  # the listing has been cancelled
        self.started_at: float | None = None

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        self.listings += 1
        self.started_at = asyncio.get_running_loop().time()
        self.entered.set()
        try:
            if self.hang:
                await asyncio.Event().wait()
            if self.remote_raises is not None:
                raise self.remote_raises
            for tool in self.tools:
                yield tool
        except asyncio.CancelledError:
            self.listing_cancelled = True
            self.cancel_seen.set()
            if self.teardown is not None:
                await self.teardown.wait()
            raise


async def _listing_started(*remotes: _Remote) -> None:
    """Wait until every one of the remotes' listings is running (so a cancel lands on a listing, not before it)."""
    await asyncio.wait_for(asyncio.gather(*(remote.entered.wait() for remote in remotes)), 5)


async def _no_tasks_beyond(before: set[asyncio.Task], within: float = 3.0) -> None:
    """Wait until nothing that was not running before is still running; fails after ``within`` seconds."""
    deadline = time.monotonic() + within
    while asyncio.all_tasks() - before:
        assert time.monotonic() < deadline, f"still running: {asyncio.all_tasks() - before}"
        await asyncio.sleep(0.01)


def _manager(**toolsets) -> ToolExecutionManager:
    return ToolExecutionManager(toolset_providers=toolsets, initiated_by=PrincipalRef.system())  # type: ignore[arg-type]


# ---- A remote toolset that cannot list is left out ---------------------------------------------------------------


async def test_a_remote_toolset_that_fails_to_list_is_left_out_with_its_reason():
    mgr = _manager(misc=_Local([TOOL_A]), kb=_Remote(raises=NetworkError("could not connect to the MCP server: refused")))
    assert [t.id for t in await mgr.list_tools()] == ["misc__a"]
    assert mgr.unavailable_toolsets() == {"kb": "could not connect to the MCP server: refused"}


async def test_a_call_into_a_left_out_toolset_answers_the_reason():
    mgr = _manager(misc=_Local([TOOL_A]), kb=_Remote(raises=NetworkError("could not connect to the MCP server: refused")))
    rp = await mgr.execute(ToolCallPart(id="c1", name="kb__search", arguments={}))
    assert rp.id == "c1"
    assert rp.error and rp.output == "toolset kb is unavailable this turn: could not connect to the MCP server: refused"


async def test_a_call_into_a_toolset_that_is_not_left_out_is_still_unknown():
    """Only a left-out toolset's tools get the reason; a name that belongs to nothing is refused exactly as before."""
    from primer.model.except_ import UnsupportedContentError

    mgr = _manager(misc=_Local([TOOL_A]), kb=_Remote(raises=NetworkError("down")))
    await mgr.list_tools()
    with pytest.raises(UnsupportedContentError):
        await mgr.execute(ToolCallPart(id="c1", name="elsewhere__search", arguments={}))
    with pytest.raises(UnsupportedContentError):
        await mgr.execute(ToolCallPart(id="c2", name="kbx__search", arguments={}))


async def test_the_longest_left_out_toolset_id_names_a_call():
    """A toolset id may itself hold the separator; the call goes to the most specific left-out id."""
    mgr = _manager(
        a=_Remote(raises=NetworkError("a is down")),
        a__b=_Remote(raises=NetworkError("a__b is down")),
    )
    rp = await mgr.execute(ToolCallPart(id="c1", name="a__b__tool", arguments={}))
    assert rp.output == "toolset a__b is unavailable this turn: a__b is down"


async def test_a_needs_sign_in_listing_is_left_out_like_any_other_failure():
    err = AuthRequiredError("the MCP server needs an operator to sign in", auth_url="https://auth.example/x", state="s")
    mgr = _manager(misc=_Local([TOOL_A]), kb=_Remote(raises=err))
    assert [t.id for t in await mgr.list_tools()] == ["misc__a"]
    assert mgr.unavailable_toolsets() == {"kb": "the MCP server needs an operator to sign in"}


async def test_a_remote_toolset_that_lists_is_offered_and_not_reported():
    mgr = _manager(misc=_Local([TOOL_A]), kb=_Remote([TOOL_SEARCH]))
    assert [t.id for t in await mgr.list_tools()] == ["misc__a", "kb__search"]
    assert mgr.unavailable_toolsets() == {}


async def test_the_catalogue_keeps_the_order_of_the_toolsets():
    """The remote listings run first, but the catalogue is still built in the order the toolsets were registered."""
    mgr = _manager(first=_Local([_tool("x", "first")]), kb=_Remote([TOOL_SEARCH]), last=_Local([_tool("y", "last")]))
    assert [t.id for t in await mgr.list_tools()] == ["first__x", "kb__search", "last__y"]


async def test_unavailable_toolsets_is_a_copy():
    mgr = _manager(kb=_Remote(raises=NetworkError("down")))
    await mgr.list_tools()
    mgr.unavailable_toolsets()["other"] = "x"
    assert mgr.unavailable_toolsets() == {"kb": "down"}


async def test_the_listing_is_kept_for_the_life_of_the_manager_and_redone_by_the_next_one():
    """No tool-list cache across turns: the manager is built per turn, so a recovered server is offered again at once."""
    kb = _Remote([TOOL_SEARCH], raises=NetworkError("down"))
    first = _manager(kb=kb)
    await first.list_tools()
    await first.list_tools()
    assert kb.listings == 1, "one manager lists once"
    assert first.unavailable_toolsets() == {"kb": "down"}

    kb.remote_raises = None
    second = _manager(kb=kb)
    assert [t.id for t in await second.list_tools()] == ["kb__search"]
    assert second.unavailable_toolsets() == {}
    assert kb.listings == 2


async def test_a_local_toolset_that_fails_to_list_still_fails():
    """Only providers that list remotely are tolerated: an in-process listing that raises is a bug and ends the turn."""
    mgr = _manager(misc=_Local(raises=ProviderError("x")), kb=_Remote([TOOL_SEARCH]))
    with pytest.raises(ProviderError):
        await mgr.list_tools()


async def test_a_provider_that_says_it_does_not_list_remotely_still_fails():
    class _Explicit(_Local):
        lists_remotely = False

    mgr = _manager(misc=_Explicit(raises=ProviderError("x")))
    with pytest.raises(ProviderError):
        await mgr.list_tools()


async def test_a_remote_tool_id_with_the_scope_separator_still_fails_the_turn():
    """The ConfigError is raised by the manager, not by the provider: it is a bug in the catalogue, not a toolset that cannot list."""
    mgr = _manager(kb=_Remote([_tool("a__b", "kb")]))
    with pytest.raises(ConfigError):
        await mgr.list_tools()


async def test_the_workspace_ext_toolset_is_not_listed_on_a_chat():
    """The chat suppression stays where it was: the skipped toolset is not even asked."""
    ext = _Remote([_tool("sleep", "workspace_ext")])
    mgr = _manager(workspace_ext=ext)
    assert await mgr.list_tools() == []
    assert ext.listings == 0
    assert mgr.unavailable_toolsets() == {}


# ---- The wait is bounded and concurrent ---------------------------------------------------------------------------


def test_the_default_bound_is_sixty_seconds():
    assert TURN_TOOLSET_LIST_TIMEOUT_S == 60.0


async def test_a_remote_listing_is_bounded(monkeypatch):
    monkeypatch.setattr(tool_listing, "TURN_TOOLSET_LIST_TIMEOUT_S", 0.05)
    remote = _Remote(hang=True)
    mgr = _manager(misc=_Local([TOOL_A]), kb=remote)
    started = time.monotonic()
    async with asyncio.timeout(1):  # the test's own net: a wait that is not bounded fails here instead of hanging
        tools = await mgr.list_tools()
    assert time.monotonic() - started < 1
    assert [t.id for t in tools] == ["misc__a"]
    assert mgr.unavailable_toolsets() == {"kb": "did not list its tools within 0.05s"}
    assert remote.listing_cancelled, "the abandoned listing is torn down, not left running"


async def test_remote_listings_run_concurrently(monkeypatch):
    monkeypatch.setattr(tool_listing, "TURN_TOOLSET_LIST_TIMEOUT_S", 0.3)
    one, two = _Remote(hang=True), _Remote(hang=True)
    mgr = _manager(one=one, two=two)
    await mgr.list_tools()
    assert mgr.unavailable_toolsets().keys() == {"one", "two"}
    # Listed one after the other, the second would only start once the first had timed out, 0.3 s later.
    assert one.started_at is not None and two.started_at is not None
    assert abs(one.started_at - two.started_at) < 0.15


async def test_the_timeout_is_read_when_the_turn_lists_not_when_the_module_loads(monkeypatch):
    remote = _Remote(hang=True)
    monkeypatch.setattr(tool_listing, "TURN_TOOLSET_LIST_TIMEOUT_S", 0.02)
    mgr = _manager(kb=remote)
    await asyncio.wait_for(mgr.list_tools(), 1)
    assert mgr.unavailable_toolsets() == {"kb": "did not list its tools within 0.02s"}


# ---- A real cancellation is never swallowed (the core of the review) ---------------------------------------------


async def test_a_real_cancellation_is_not_swallowed():
    before = asyncio.all_tasks()
    remote = _Remote(hang=True)
    mgr = _manager(misc=_Local([TOOL_A]), kb=remote)
    task = asyncio.create_task(mgr.list_tools())
    await _listing_started(remote)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert remote.listing_cancelled, "the listing was cancelled and had finished before the cancel reached the caller"
    assert mgr.unavailable_toolsets() == {}, "a cancelled listing leaves nothing behind"
    assert asyncio.all_tasks() - before == set(), "no listing task outlives the cancelled wait"


async def test_a_cancellation_reaches_every_listing_that_is_waiting():
    one, two = _Remote(hang=True), _Remote(hang=True)
    mgr = _manager(one=one, two=two)
    task = asyncio.create_task(mgr.list_tools())
    await _listing_started(one, two)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert one.listing_cancelled and two.listing_cancelled


async def test_a_cancelled_listing_does_not_poison_the_next_try():
    """The manager is not left half-built: after a cancelled listing the same manager lists again from scratch."""
    remote = _Remote([TOOL_SEARCH], hang=True)
    mgr = _manager(kb=remote)
    task = asyncio.create_task(mgr.list_tools())
    await _listing_started(remote)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    remote.hang = False
    assert [t.id for t in await mgr.list_tools()] == ["kb__search"]


async def test_a_stop_scope_around_the_wait_ends_it_at_once():
    """A Stop is delivered into an await by ``interruptible`` (``asyncio.timeout``): it cancels the waiting task and converts the
    CancelledError that comes back into ``Interrupted``. That conversion counts the cancels the task carries, so the wait must
    neither swallow the cancellation nor ``uncancel()`` it: either way the scope would not raise ``Interrupted``."""
    stop = asyncio.Event()
    remote = _Remote(hang=True)
    mgr = _manager(kb=remote)

    async def stop_once_listing() -> None:
        await _listing_started(remote)
        stop.set()

    stopper = asyncio.create_task(stop_once_listing())
    started = time.monotonic()
    with pytest.raises(Interrupted):
        async with asyncio.timeout(2):  # the test's own net: a swallowed Stop ends here as a TimeoutError instead
            async with interruptible(stop):
                await mgr.list_tools()
    await stopper
    assert time.monotonic() - started < 1
    assert remote.listing_cancelled
    task = asyncio.current_task()
    assert task is not None and task.cancelling() == 0, "the scope settled its own cancel and nothing else is pending"


async def test_a_second_cancel_while_the_listing_is_tearing_down_still_propagates():
    """A listing that is slow to die must not make the wait hold the caller: a cancel that arrives while it unwinds is the caller's too."""
    release = asyncio.Event()
    before = asyncio.all_tasks()
    remote = _Remote(hang=True, teardown=release)
    mgr = _manager(kb=remote)
    task = asyncio.create_task(mgr.list_tools())
    await _listing_started(remote)
    task.cancel()
    await asyncio.wait_for(remote.cancel_seen.wait(), 5)
    assert not task.done(), "the wait lets the listing finish tearing down before the cancel goes on"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    release.set()  # the abandoned listing now finishes by itself
    await _no_tasks_beyond(before)


async def test_a_listing_that_cancels_itself_is_a_failed_listing_not_a_cancelled_turn():
    """The MCP SDK's anyio scopes (and the stdio handshake) can end a listing with a CancelledError of their own: the toolset is
    left out, and the task that asked is NOT cancelled (it is not the caller's cancellation)."""

    class _TornDown(_Remote):
        async def list_tools(self, *, principal=None):
            raise asyncio.CancelledError
            yield  # pragma: no cover - makes this an async generator

    mgr = _manager(misc=_Local([TOOL_A]), kb=_TornDown())
    assert [t.id for t in await mgr.list_tools()] == ["misc__a"]
    assert mgr.unavailable_toolsets() == {"kb": "its connection was torn down while listing"}
    task = asyncio.current_task()
    assert task is not None and task.cancelling() == 0


async def test_a_group_of_cancellations_is_a_failed_listing_too():
    class _GroupOfCancels(_Remote):
        async def list_tools(self, *, principal=None):
            raise BaseExceptionGroup("unhandled errors in a TaskGroup", [asyncio.CancelledError()])
            yield  # pragma: no cover - makes this an async generator

    mgr = _manager(kb=_GroupOfCancels())
    await mgr.list_tools()
    assert mgr.unavailable_toolsets() == {"kb": "its connection was torn down while listing"}


async def test_a_group_with_a_real_failure_reports_the_failure():
    class _GroupWithFailure(_Remote):
        async def list_tools(self, *, principal=None):
            raise ExceptionGroup("unhandled errors in a TaskGroup", [NetworkError("could not connect to the MCP server: refused")])
            yield  # pragma: no cover - makes this an async generator

    mgr = _manager(kb=_GroupWithFailure())
    await mgr.list_tools()
    assert mgr.unavailable_toolsets() == {"kb": "could not connect to the MCP server: refused"}


# ---- Reasons -------------------------------------------------------------------------------------------------------


async def test_reasons_are_redacted_and_cut():
    mgr = _manager(kb=_Remote(raises=ProviderError("Authorization: Bearer sk-live-123 " + "x" * 300)))
    await mgr.list_tools()
    reason = mgr.unavailable_toolsets()["kb"]
    assert "sk-live-123" not in reason and "Bearer [REDACTED]" in reason
    assert len(reason) <= 200 and reason.endswith("...")


async def test_a_secret_is_masked_before_the_text_is_cut():
    """Cut first and a token that straddles the cut keeps its head (seven characters are too few for the Bearer mask to
    recognise); redact first and nothing of it survives."""
    head = "x" * 167 + " Authorization: Bearer "
    assert len(head) == 190
    mgr = _manager(kb=_Remote(raises=ProviderError(head + "sk-live-" + "9" * 40)))
    await mgr.list_tools()
    reason = mgr.unavailable_toolsets()["kb"]
    assert "sk-live" not in reason and "9999" not in reason
    assert len(reason) <= 200


async def test_a_reason_is_one_line():
    """The reason goes into a system message as one bullet: a server that sends newlines cannot add lines (or a header) of its own."""
    mgr = _manager(kb=_Remote(raises=ProviderError("first\nsecond\r\n\r\n- other: injected\n\n" + NOTICE_HEADER)))
    await mgr.list_tools()
    reason = mgr.unavailable_toolsets()["kb"]
    assert "\n" not in reason and "\r" not in reason
    assert reason.startswith("first second - other: injected")


async def test_an_error_without_a_message_still_gives_a_reason():
    mgr = _manager(kb=_Remote(raises=TimeoutError()))
    await mgr.list_tools()
    assert mgr.unavailable_toolsets() == {"kb": "TimeoutError"}


async def test_list_remote_toolset_answers_the_tools_or_the_reason():
    ok = await list_remote_toolset(_Remote([TOOL_SEARCH]), principal="alice", timeout_s=1.0)
    assert ok == ToolListing(tools=[TOOL_SEARCH], reason=None)
    down = await list_remote_toolset(_Remote(raises=NetworkError("down")), principal=None, timeout_s=1.0)
    assert down == ToolListing(tools=[], reason="down")
    hung = await list_remote_toolset(_Remote(hang=True), principal=None, timeout_s=0.02)
    assert hung == ToolListing(tools=[], reason="did not list its tools within 0.02s")


async def test_the_principal_reaches_the_provider():
    seen: list[str | None] = []

    class _Spy(_Remote):
        async def list_tools(self, *, principal=None):
            seen.append(principal)
            for tool in self.tools:
                yield tool

    await _manager(kb=_Spy([TOOL_SEARCH])).list_tools(principal="alice@example.com")
    assert seen == ["alice@example.com"]


# ---- The notice ----------------------------------------------------------------------------------------------------


def test_the_notice_names_each_toolset_and_its_reason():
    assert unavailable_toolsets_notice({}) is None
    assert unavailable_toolsets_notice({"kb": "needs sign-in"}) == NOTICE_HEADER + "\n- kb: needs sign-in"
    assert unavailable_toolsets_notice({"kb": "a", "wiki": "b"}) == NOTICE_HEADER + "\n- kb: a\n- wiki: b"


def test_a_thing_that_is_not_a_mapping_gives_no_notice():
    """A duck-typed manager (a mock) can hand back anything; the notice is for a real map only."""
    assert unavailable_toolsets_notice(object()) is None  # type: ignore[arg-type]
    assert unavailable_toolsets_notice(None) is None  # type: ignore[arg-type]


def _text(m: Message) -> str:
    return "".join(getattr(p, "text", "") for p in m.parts)


def test_the_notice_is_appended_to_the_first_system_message():
    system = Message(role="system", parts=[TextPart(text="You are X.")])
    user = Message(role="user", parts=[TextPart(text="go")])
    out = with_system_notice([system, user], "N")
    assert [m.role for m in out] == ["system", "user"]
    assert _text(out[0]) == "You are X.\n\nN"
    assert out[1] is user
    assert _text(system) == "You are X.", "the message it was given is not changed"


def test_without_a_system_message_the_notice_is_inserted_first():
    user = Message(role="user", parts=[TextPart(text="go")])
    assert with_system_notice([user], "N") == [Message(role="system", parts=[TextPart(text="N")]), user]


def test_no_notice_returns_the_same_list():
    messages = [Message(role="user", parts=[TextPart(text="go")])]
    assert with_system_notice(messages, None) is messages
    assert with_system_notice(messages, "") is messages


def test_the_first_system_message_is_the_one_extended():
    first = Message(role="system", parts=[TextPart(text="one")])
    second = Message(role="system", parts=[TextPart(text="two")])
    user = Message(role="user", parts=[TextPart(text="go")])
    out = with_system_notice([user, first, second], "N")
    assert [_text(m) for m in out] == ["go", "one\n\nN", "two"]


def test_a_system_message_of_several_parts_gets_the_notice_after_its_last_text():
    system = Message(role="system", parts=[TextPart(text="one"), TextPart(text="two")])
    out = with_system_notice([system], "N")
    assert [p.text for p in out[0].parts] == ["one", "two\n\nN"]


# ---- Through the loop ----------------------------------------------------------------------------------------------


class _NoticeManager(_Manager):
    """The seam test's manager, with a map of toolsets left out."""

    def __init__(self, unavailable: dict[str, str]) -> None:
        super().__init__()
        self._unavailable = unavailable

    def unavailable_toolsets(self) -> dict[str, str]:
        return dict(self._unavailable)


def _prompt(*, system: bool) -> list[Message]:
    prompt = [Message(role="user", parts=[TextPart(text="go")])]
    if system:
        prompt.insert(0, Message(role="system", parts=[TextPart(text="You are X.")]))
    return prompt


async def _run(manager, prompt, *, tool_rounds=1) -> tuple[_RoundsLLM, list[Message], list]:
    llm = _RoundsLLM(tool_rounds)
    messages_out: list[Message] = []
    events = [
        ev
        async for ev in run_agent_turn(
            agent=AGENT, llm=llm, llm_model=MODEL, tool_manager=manager, prompt=prompt, messages_out=messages_out,
        )
    ]
    return llm, messages_out, events


async def test_the_notice_reaches_the_model_and_is_not_persisted():
    prompt = _prompt(system=True)
    llm, messages_out, _ = await _run(_NoticeManager({"kb": "needs sign-in"}), prompt)
    assert len(llm.sent) == 2, "a tool round and an answer: two model calls"
    for call in llm.sent:
        assert call["messages"][0].parts[0].text == "You are X.\n\n" + NOTICE_HEADER + "\n- kb: needs sign-in"
    assert not any(NOTICE_HEADER in getattr(p, "text", "") for m in messages_out for p in m.parts)
    assert prompt[0].parts[0].text == "You are X.", "the caller's prompt is not changed either"


async def test_without_a_system_prompt_the_notice_is_its_own_system_message():
    prompt = _prompt(system=False)
    llm, messages_out, _ = await _run(_NoticeManager({"kb": "needs sign-in"}), prompt)
    assert llm.sent[0]["messages"][0] == Message(role="system", parts=[TextPart(text=NOTICE_HEADER + "\n- kb: needs sign-in")])
    assert llm.sent[0]["messages"][1:] == prompt
    assert all(m.role != "system" for m in messages_out)


async def test_a_manager_without_the_method_sends_the_prompt_unchanged():
    prompt = _prompt(system=True)
    llm, _, _ = await _run(_Manager(), prompt)
    assert llm.sent[0]["messages"] == prompt


async def test_a_manager_with_nothing_left_out_sends_the_prompt_unchanged():
    prompt = _prompt(system=True)
    llm, _, _ = await _run(_NoticeManager({}), prompt)
    assert llm.sent[0]["messages"] == prompt


async def test_a_turn_with_an_unreachable_mcp_toolset_completes():
    mgr = _manager(misc=_Local([TOOL_A]), kb=_Remote(raises=NetworkError("could not connect to the MCP server: refused")))
    llm, messages_out, events = await _run(mgr, _prompt(system=True), tool_rounds=0)
    assert events[-1] == Done(stop_reason="stop", raw_reason="stop")
    assert [t.id for t in llm.sent[0]["tools"]] == ["misc__a"]
    assert llm.sent[0]["messages"][0].parts[0].text == (
        "You are X.\n\n" + NOTICE_HEADER + "\n- kb: could not connect to the MCP server: refused"
    )
    assert [m.role for m in messages_out] == ["assistant"]


async def test_a_turn_whose_loop_lists_the_tools_itself_gets_the_notice_too():
    """``run_agent_turn`` without ``tools=`` (graph nodes, subagents) lists through the manager and reads the same map."""
    mgr = _manager(kb=_Remote(raises=NetworkError("down")))
    llm, _, _ = await _run(mgr, _prompt(system=False), tool_rounds=0)
    assert llm.sent[0]["tools"] == []
    assert llm.sent[0]["messages"][0] == Message(role="system", parts=[TextPart(text=NOTICE_HEADER + "\n- kb: down")])


# ---- The real MCP provider -----------------------------------------------------------------------------------------


def _http_provider(port: int) -> McpToolsetProvider:
    return McpToolsetProvider(
        toolset_id="kb",
        config=McpConfig(transport=TransportType.HTTP, config=HttpConfig(url=f"http://127.0.0.1:{port}/mcp")),
    )


def test_only_the_mcp_provider_lists_remotely():
    assert ToolsetProvider.lists_remotely is False
    assert McpToolsetProvider.lists_remotely is True
    from primer.agent.external_tools import ExternalToolsetProvider
    from primer.toolset.client import ClientToolsetProvider
    from primer.toolset.internal import InternalToolsetProvider
    from primer.toolset.python_runner.provider import PythonToolsetProvider

    for local in (ExternalToolsetProvider, ClientToolsetProvider, InternalToolsetProvider, PythonToolsetProvider):
        assert local.lists_remotely is False, local.__name__


async def test_a_refused_connection_leaves_the_real_mcp_toolset_out():
    mgr = _manager(misc=_Local([TOOL_A]), kb=_http_provider(1))
    async with asyncio.timeout(20):
        assert [t.id for t in await mgr.list_tools()] == ["misc__a"]
    assert mgr.unavailable_toolsets()["kb"].startswith("could not connect to the MCP server")


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A TCP server that accepts a connection, reads nothing back to it and never answers: the MCP server that hangs."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.05)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except (TimeoutError, OSError):
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join(2)
        for conn in held:
            conn.close()
        server.close()


async def test_a_server_that_accepts_and_never_answers_is_left_out_within_the_bound(silent_server, monkeypatch):
    monkeypatch.setattr(tool_listing, "TURN_TOOLSET_LIST_TIMEOUT_S", 0.5)
    before = asyncio.all_tasks()
    mgr = _manager(misc=_Local([TOOL_A]), kb=_http_provider(silent_server))
    started = time.monotonic()
    async with asyncio.timeout(15):
        tools = await mgr.list_tools()
    assert time.monotonic() - started < 10
    assert [t.id for t in tools] == ["misc__a"]
    assert mgr.unavailable_toolsets() == {"kb": "did not list its tools within 0.5s"}
    assert asyncio.all_tasks() - before == set(), "the SDK's transport tasks went down with the listing"


async def test_a_cancel_while_the_real_server_hangs_is_not_swallowed(silent_server):
    before = asyncio.all_tasks()
    mgr = _manager(kb=_http_provider(silent_server))
    task = asyncio.create_task(mgr.list_tools())
    await asyncio.sleep(0.3)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert task.cancelled()
    assert asyncio.all_tasks() - before == set()


async def test_a_stop_during_a_subagents_turn_start_listing_ends_it_before_the_model(monkeypatch):
    """End to end through the REAL stdio provider, the incident class of stop slice B1 (tests/agent/test_subagent_stop.py).

    The MCP stdio handshake answers a cancel by clearing every pending cancel on its task and raising ConfigError, so a
    listing run in the turn's own task would take the operator's Stop for a toolset that failed to list and carry on to
    the model. Here the server is a process that never answers ``initialize``, so it is the LISTING that hangs, at the
    start of a subagent's turn; the Stop reaches it the way it does in production, as the cancel ``run_stoppable`` sends the
    call's task. The subagent must end without a model call (and without running anything), and the launched server must
    go down with the listing."""
    import primer.agent.invoke as invoke
    from primer.agent.invoke import run_subagent
    from primer.agent.stoppable_call import run_stoppable

    marker = f"primer-listing-{uuid.uuid4().hex}"
    llm = subagent_stop._ScriptedLlm([None])  # would answer at once, if the subagent ever reached it
    recorder = subagent_stop._Recorder()
    provider = McpToolsetProvider(
        toolset_id="ext",
        config=McpConfig(
            transport=TransportType.STDIO,
            # Not an MCP server: it starts, reads nothing and never answers ``initialize``.
            config=StdioConfig(command=[sys.executable, "-c", f"import time; time.sleep(120)  # {marker}"]),
        ),
    )
    manager = ToolExecutionManager(
        toolset_providers={"ext": provider, "rec": recorder},  # type: ignore[arg-type]
        initiated_by=PrincipalRef(type="user", id="u1", display="u1", role="user", source="local"),
    )

    async def resolve(agent_id, *, storage_provider, provider_registry):
        return subagent_stop.AGENT, llm, subagent_stop.MODEL

    async def build(context, *, storage_provider=None, provider_registry=None, approval_resolver=None):
        return manager

    monkeypatch.setattr(invoke, "_resolve_agent_runtime", resolve)
    monkeypatch.setattr(invoke, "build_subagent_toolmanager", build)
    stop = asyncio.Event()
    call = asyncio.create_task(
        run_stoppable(
            lambda: run_subagent(agent_id="sub", prompt="go", storage_provider=None, provider_registry=None),
            interrupt=stop, interruptible=lambda: True, name="invoke_agent",
        )
    )
    try:
        await subagent_stop._until(lambda: subagent_stop._marked_pids(marker) or call.done())  # the handshake is in flight
        assert not call.done(), f"the subagent ended before it reached the listing: {call.exception()!r}"
        assert llm.requests == 0, "the subagent was not inside the listing: not the situation"

        stop.set()
        result = await asyncio.wait_for(call, timeout=30.0)

        # None is "the Stop fired and the call has no usable result": the parent answers it ``interrupted: stopped by user``.
        assert result is None, f"the parent was handed {result!r} as the call's result"
        assert llm.requests == 0, "the subagent went on to the model after the Stop"
        assert recorder.executed == []
        await subagent_stop._until(lambda: not subagent_stop._marked_pids(marker))  # the server went down with the listing
    finally:
        stop.set()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        subagent_stop._kill_marked(marker)
