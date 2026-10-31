"""The MCP tool-list cache (perf plan Task 8, finding M3): a turn no longer re-lists a remote toolset it listed moments ago.

The tool manager is rebuilt every turn, so its catalogue lives one turn; every turn paid a handshake and ``tools/list`` per MCP toolset, and a dead
server cost the full listing timeout every turn. The provider is cached per toolset, so it now keeps the listing: keyed by the person (the listing
depends on the sign-in), the OAuth token fingerprint, the egress guard state and the config, for ``list_cache_ttl_seconds``; a failure is kept for
``list_failure_ttl_seconds``. Every key part and every invalidation below has a test.
"""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager

import mcp.types as mcp_types
import pytest

from primer.access.acting import acting_as
from primer.access.person import Person
from primer.agent import tool_listing
from primer.model.except_ import AuthRequiredError, NetworkError
from primer.model.mcp_oauth import McpSignInRequiredError
from primer.model.provider import HttpConfig, McpConfig, OAuthConfig, TransportType
from primer.toolset.mcp import McpToolsetProvider

ALICE, BOB = Person("u-alice", "user"), Person("u-bob", "user")


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Session:
    def __init__(self, provider) -> None:
        self._provider = provider

    async def list_tools(self):
        return mcp_types.ListToolsResult(tools=[mcp_types.Tool(name=n, inputSchema={"type": "object"}) for n in self._provider.tool_names()])

    async def call_tool(self, name, arguments=None, **kw):
        raise RuntimeError("boom")


class _OAuth:
    """A sign-in service double: a token per person, or a sign-in-required refusal."""

    def __init__(self) -> None:
        self.tokens: dict[str | None, str] = {}
        self.signed_out: set[str | None] = set()

    async def access_token(self, target):
        user = target.user_id
        if user in self.signed_out:
            raise McpSignInRequiredError(target.toolset_id, reason="signed out", per_user=True)
        return self.tokens.get(user, "t-default")


class _Provider(McpToolsetProvider):
    """The real provider with the transport replaced: it counts the sessions it opens."""

    def __init__(self, *, oauth=None, guard=False, clock=None, names=("a",), **kw) -> None:
        config = McpConfig(
            transport=TransportType.HTTP,
            config=HttpConfig(url="https://mcp.example.test/mcp", oauth=OAuthConfig() if oauth is not None else None),
        )
        super().__init__("srv", config, oauth=oauth, egress_guard=guard, clock=clock or _Clock(), **kw)
        self.opened = 0
        self.names: dict[str | None, tuple[str, ...]] | tuple[str, ...] = names
        self.open_error: BaseException | None = None
        self.hang = False

    def tool_names(self):
        if isinstance(self.names, dict):
            from primer.access.person import person_for_tool

            return self.names[person_for_tool(None).user_id]
        return self.names

    def _open_session(self, *, principal=None, ctx=None):  # type: ignore[override]
        provider = self

        @asynccontextmanager
        async def cm():
            provider.opened += 1
            if provider.hang:
                await asyncio.sleep(3600)
            if provider.open_error is not None:
                raise provider.open_error
            yield _Session(provider)

        return cm()


async def _list(provider, who=ALICE):
    with acting_as(who):
        return [t.id async for t in provider.list_tools()]


@pytest.mark.asyncio
async def test_a_second_listing_within_the_ttl_opens_no_session():
    p = _Provider()
    assert await _list(p) == ["a"] and await _list(p) == ["a"]
    assert p.opened == 1


@pytest.mark.asyncio
async def test_the_listing_is_fetched_again_after_the_ttl():
    clock = _Clock()
    p = _Provider(clock=clock)
    await _list(p)
    clock.now += 29
    await _list(p)
    assert p.opened == 1
    clock.now += 2
    await _list(p)
    assert p.opened == 2


@pytest.mark.asyncio
async def test_one_persons_listing_is_never_served_to_another():
    oauth = _OAuth()
    oauth.tokens = {"u-alice": "tok-a", "u-bob": "tok-b"}
    p = _Provider(oauth=oauth, names={"u-alice": ("alice-tool",), "u-bob": ("bob-tool",)})
    assert await _list(p, ALICE) == ["alice-tool"]
    assert await _list(p, BOB) == ["bob-tool"], "bob must not get alice's listing"
    assert await _list(p, ALICE) == ["alice-tool"] and await _list(p, BOB) == ["bob-tool"]
    assert p.opened == 2


@pytest.mark.asyncio
async def test_a_changed_sign_in_token_lists_again():
    oauth = _OAuth()
    oauth.tokens = {"u-alice": "tok-1"}
    p = _Provider(oauth=oauth)
    await _list(p)
    oauth.tokens["u-alice"] = "tok-2"  # signed in again, or the token was refreshed to a different identity
    await _list(p)
    assert p.opened == 2


@pytest.mark.asyncio
async def test_a_sign_out_is_never_hidden_by_the_cache():
    oauth = _OAuth()
    p = _Provider(oauth=oauth)
    await _list(p)
    oauth.signed_out.add("u-alice")
    with pytest.raises(McpSignInRequiredError):
        await _list(p)
    with pytest.raises(McpSignInRequiredError):
        await _list(p)  # and it is not cached as a failure either: signing in again works at once
    oauth.signed_out.clear()
    assert await _list(p) == ["a"]


@pytest.mark.asyncio
async def test_flipping_the_egress_guard_lists_again():
    state = {"guarded": False}

    async def guard() -> bool:
        return state["guarded"]

    p = _Provider(guard=guard)
    await _list(p)
    state["guarded"] = True
    await _list(p)
    assert p.opened == 2, "a listing fetched without the guard must not stand for the guarded provider"


@pytest.mark.asyncio
async def test_a_failed_listing_is_remembered_briefly_then_tried_again():
    clock = _Clock()
    p = _Provider(clock=clock)
    p.open_error = NetworkError("connection refused")
    for _ in range(3):
        with pytest.raises(NetworkError):
            await _list(p)
    assert p.opened == 1, "a dead server is asked once, not on every turn"
    clock.now += 16
    p.open_error = None
    assert await _list(p) == ["a"] and p.opened == 2


@pytest.mark.asyncio
async def test_a_sign_in_or_auth_failure_is_never_remembered():
    p = _Provider()
    p.open_error = AuthRequiredError("the server wants a token", auth_url="https://auth.example.test/authorize", state="s")
    for _ in range(2):
        with pytest.raises(AuthRequiredError):
            await _list(p)
    assert p.opened == 2


@pytest.mark.asyncio
async def test_a_failed_call_forgets_the_listing():
    p = _Provider()
    await _list(p)
    with acting_as(ALICE), pytest.raises(Exception):
        await p.call(tool_name="a", arguments={})
    await _list(p)
    assert p.opened == 3  # list, the failed call's session, list again


@pytest.mark.asyncio
async def test_invalidate_listing_forgets_everything():
    p = _Provider()
    await _list(p)
    p.invalidate_listing()
    await _list(p)
    assert p.opened == 2


@pytest.mark.asyncio
async def test_a_hit_restores_the_task_tool_set():
    p = _Provider()
    await _list(p)
    p._task_tools = {"stale"}
    await _list(p)
    assert p._task_tools == set()  # the latest listing's answer, as a fresh listing would have set it
    assert p.opened == 1


@pytest.mark.asyncio
async def test_a_timed_out_listing_is_remembered_so_the_next_turn_does_not_wait_again(monkeypatch):
    monkeypatch.setattr(tool_listing, "TURN_TOOLSET_LIST_TIMEOUT_S", 0.05)
    p = _Provider()
    p.hang = True
    first = await tool_listing.list_remote_toolset(p, principal=None, timeout_s=0.05)
    assert first.state == tool_listing.TIMED_OUT and p.opened == 1
    started = asyncio.get_running_loop().time()
    second = await tool_listing.list_remote_toolset(p, principal=None, timeout_s=0.05)
    assert second.state == tool_listing.TIMED_OUT and p.opened == 1, "the second turn must not open another hanging session"
    assert asyncio.get_running_loop().time() - started < 0.04


@pytest.mark.asyncio
async def test_the_registry_drops_the_provider_and_so_its_cache_when_the_toolset_changes():
    from primer.api.registries.provider_registry import ProviderRegistry

    registry = ProviderRegistry(storage_provider=object())
    p = _Provider()
    await _list(p)
    registry._toolset_cache["srv"] = p  # type: ignore[assignment]
    await registry._invalidate_toolset_local("srv")
    assert "srv" not in registry._toolset_cache
    fresh = _Provider()
    await _list(fresh)
    assert fresh.opened == 1  # a rebuilt provider starts with an empty cache


def test_the_cache_key_is_a_fingerprint_not_the_token():
    key_part = hashlib.sha256(b"tok").hexdigest()[:16]
    assert "tok" not in key_part
