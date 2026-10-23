"""Token use and refresh that is safe across server processes (plan 07, Task 8; master Review Focus 3; plan Review Focus 1 and 4).

Many authorization servers rotate refresh tokens: a refresh token works ONCE. Two processes that refresh the same toolset at once would spend it twice,
and the loser would sign the toolset out for no reason. The service refreshes under a lease taken by compare-and-set on the token row, so one process
exchanges while the others wait for its answer; the stored refresh token is always the newest one.

Against the hermetic mock (which rotates refresh tokens and refuses a reused one), over the in-memory storage fake, with a clock the test moves. Two
services on one storage are two server processes. The Postgres twin, with two connection pools, is ``tests/storage/test_mcp_oauth_refresh_race_live.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from pydantic import SecretStr

from primer.common.secret_box import SecretBox
from primer.model.except_ import ProviderError
from primer.model.mcp_oauth import McpSignInRequiredError
from primer.storage import raw_generation
from primer.toolset.oauth import service as service_module
from primer.toolset.oauth.service import (
    EXPIRED_NO_REFRESH,
    KEY_CHANGED,
    LEASE_TTL,
    NOT_CONNECTED,
    REFRESH_SKEW,
    SETTINGS_CHANGED,
    TOKEN_REJECTED,
    ToolsetOAuthService,
    config_fingerprint,
)
from primer.toolset.oauth.store import McpOAuthStore
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401 - the fixture
from tests.toolset.oauth._service_helpers import KEY, OTHER_KEY, Clock, connect, make_toolset, mcp_answers, target_of, token_row

CC = {"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "cc-secret"}}
#: Every wait in this file is bounded, so a refresh that never ends fails its test instead of hanging the lane.
_BOUND_S = 30.0


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def box() -> SecretBox:
    return SecretBox.from_key_material(KEY)


@pytest.fixture
def store(fake_storage_provider, box) -> McpOAuthStore:
    return McpOAuthStore(fake_storage_provider, box)


@pytest.fixture
async def services(fake_storage_provider, box, clock):
    """Makes services on ONE storage: each is a server process with its own HTTP client, lock and lease owner."""
    made: list[ToolsetOAuthService] = []

    def make(owner_id: str = "pod-a", *, secret_box: SecretBox | None = None) -> ToolsetOAuthService:
        service = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=secret_box or box, clock=clock, owner_id=owner_id)
        made.append(service)
        return service

    yield make
    for service in made:
        await service.aclose()


@pytest.fixture
def svc(services) -> ToolsetOAuthService:
    return services("pod-a")


@pytest.fixture
def kb(mock_oauth_mcp):
    return make_toolset(mock_oauth_mcp.mcp_url, oauth={})


@pytest.fixture
def target(kb):
    return target_of(kb)


async def _tokens(store: McpOAuthStore, toolset_id: str = "kb") -> tuple[str | None, str | None]:
    row = await store.get_tokens(toolset_id)
    return store.unseal(row.access_token_enc), store.unseal(row.refresh_token_enc)


def _refreshes(server) -> int:
    return [request.get("grant_type") for request in server.state.token_requests].count("refresh_token")


def _near_expiry(clock: Clock) -> None:
    """Move the clock so a token issued for an hour at NOW expires in 30 s: inside REFRESH_SKEW, so it is refreshed before it is used."""
    clock.advance(minutes=59, seconds=30)
    assert timedelta(seconds=30) < REFRESH_SKEW


async def _until(condition, *, what: str) -> None:
    try:
        async with asyncio.timeout(_BOUND_S):
            while not await condition():
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"{what} never came") from None


async def _refresh_request_arrived(server) -> None:
    async def arrived() -> bool:
        return _refreshes(server) > 0

    await _until(arrived, what="the refresh request")


async def _hold_lease(store: McpOAuthStore, owner: str, until) -> None:
    row = await store.get_tokens("kb")
    held = await store.patch_tokens("kb", {"refresh_lease_until": until, "refresh_lease_owner": owner}, where={"version": [raw_generation(row, "version")]})
    assert held is not None


# ---- using the token -----------------------------------------------------------------------------------------------------------------------------------------------------


async def test_a_fresh_token_is_returned_without_a_request(svc, kb, target, store, mock_oauth_mcp):
    await connect(svc, kb)
    before = len(mock_oauth_mcp.state.token_requests)
    access, _ = await _tokens(store)
    assert await svc.access_token(target) == access
    assert len(mock_oauth_mcp.state.token_requests) == before


async def test_an_unconnected_toolset_needs_sign_in(svc, target, mock_oauth_mcp):
    with pytest.raises(McpSignInRequiredError) as caught:
        await svc.access_token(target)
    assert str(caught.value) == "the MCP server kb needs an operator to reconnect it" and caught.value.reason == NOT_CONNECTED
    assert mock_oauth_mcp.state.token_requests == []


async def test_a_token_near_expiry_is_refreshed(svc, kb, target, store, mock_oauth_mcp, clock):
    await connect(svc, kb)
    old_access, old_refresh = await _tokens(store)
    _near_expiry(clock)
    new = await svc.access_token(target)
    assert new != old_access and _refreshes(mock_oauth_mcp) == 1
    row = await store.get_tokens("kb")
    assert row.version == 2 and store.unseal(row.access_token_enc) == new and store.unseal(row.refresh_token_enc) != old_refresh
    assert (row.state, row.reason, row.refresh_lease_until, row.refresh_lease_owner, row.refreshed_at) == ("connected", None, None, None, clock.now)
    assert row.expires_at == clock.now + timedelta(hours=1)
    request = mock_oauth_mcp.state.token_requests[-1]
    assert request["refresh_token"] == old_refresh and request["resource"] == mock_oauth_mcp.mcp_url and "scope" not in request
    assert mcp_answers(mock_oauth_mcp.mcp_url, new) == 200


async def test_two_pods_refreshing_at_once_make_one_exchange(services, kb, target, store, mock_oauth_mcp, clock):
    """Review Focus 1: one exchange, every caller gets the new access token, the stored refresh token is the rotated one, the toolset stays Connected."""
    a, b = services("pod-a"), services("pod-b")
    await connect(a, kb)
    old_access, old_refresh = await _tokens(store)
    _near_expiry(clock)
    mock_oauth_mcp.state.token_delay_s = 0.2
    async with asyncio.timeout(_BOUND_S):
        tokens = await asyncio.gather(*(pod.access_token(target) for pod in (a, b, a, b, a, b)))
    assert len(set(tokens)) == 1 and tokens[0] != old_access
    assert _refreshes(mock_oauth_mcp) == 1
    row = await store.get_tokens("kb")
    assert store.unseal(row.refresh_token_enc) != old_refresh and (row.state, row.version) == ("connected", 2)
    assert row.refresh_lease_until is None and row.refresh_lease_owner is None
    assert mcp_answers(mock_oauth_mcp.mcp_url, tokens[0]) == 200
    assert (await b.status(kb)).state == "connected"


async def test_a_waiting_pod_takes_the_token_the_lease_holder_stores(svc, kb, target, store, mock_oauth_mcp, clock, monkeypatch):
    """Another process holds the lease: this one waits, makes no request of its own, and returns what the holder stored."""
    monkeypatch.setattr(service_module, "LEASE_POLL_S", 0.02)
    await connect(svc, kb)
    _near_expiry(clock)
    await _hold_lease(store, "pod-b#1", clock.now + LEASE_TTL)
    requests = len(mock_oauth_mcp.state.token_requests)
    waiting = asyncio.create_task(svc.access_token(target))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    held = await store.get_tokens("kb")
    await store.patch_tokens(
        "kb",
        {"access_token_enc": store.seal("at-from-pod-b"), "refresh_token_enc": store.seal("rt-from-pod-b"), "expires_at": clock.now + timedelta(hours=1),
         "version": held.version + 1, "refresh_lease_until": None, "refresh_lease_owner": None},
        where={"refresh_lease_owner": ["pod-b#1"]},
    )
    async with asyncio.timeout(_BOUND_S):
        assert await waiting == "at-from-pod-b"
    assert len(mock_oauth_mcp.state.token_requests) == requests


async def test_a_live_lease_held_elsewhere_makes_the_caller_give_up_in_time(svc, kb, target, store, mock_oauth_mcp, clock, monkeypatch):
    monkeypatch.setattr(service_module, "LEASE_WAIT_S", 0.3)
    monkeypatch.setattr(service_module, "LEASE_POLL_S", 0.05)
    await connect(svc, kb)
    _near_expiry(clock)
    await _hold_lease(store, "pod-b#1", clock.now + LEASE_TTL)
    requests = len(mock_oauth_mcp.state.token_requests)
    with pytest.raises(ProviderError, match=r"^the MCP server kb could not refresh its sign-in: another server process is refreshing it; try again$") as caught:
        async with asyncio.timeout(_BOUND_S):
            await svc.access_token(target)
    assert not isinstance(caught.value, McpSignInRequiredError)
    row = await store.get_tokens("kb")
    assert len(mock_oauth_mcp.state.token_requests) == requests
    assert (row.refresh_lease_owner, row.state, row.version) == ("pod-b#1", "connected", 1), "a waiter never touches a lease it does not hold"


async def test_a_dead_lease_is_taken_over(svc, kb, target, store, mock_oauth_mcp, clock):
    await connect(svc, kb)
    old_access, _ = await _tokens(store)
    _near_expiry(clock)
    await _hold_lease(store, "dead", clock.now + LEASE_TTL)
    clock.advance(seconds=31)
    async with asyncio.timeout(_BOUND_S):
        new = await svc.access_token(target)
    assert new != old_access and _refreshes(mock_oauth_mcp) == 1
    row = await store.get_tokens("kb")
    assert (row.refresh_lease_owner, row.refresh_lease_until, row.version, row.state) == (None, None, 2, "connected")


async def test_a_missing_refresh_token_in_the_answer_keeps_the_old_one(svc, kb, target, store, mock_oauth_mcp, clock):
    await connect(svc, kb)
    _, old_refresh = await _tokens(store)
    mock_oauth_mcp.state.issue_refresh_tokens = False
    _near_expiry(clock)
    first = await svc.access_token(target)
    row = await store.get_tokens("kb")
    assert store.unseal(row.refresh_token_enc) == old_refresh and row.version == 2
    clock.advance(minutes=59, seconds=40)
    second = await svc.access_token(target)
    assert second != first and _refreshes(mock_oauth_mcp) == 2, "the kept refresh token still works"
    assert store.unseal((await store.get_tokens("kb")).refresh_token_enc) == old_refresh


async def test_a_token_without_an_expiry_is_used_until_the_server_rejects_it(svc, kb, target, store, clock, mock_oauth_mcp):
    await connect(svc, kb)
    row = await store.get_tokens("kb")
    await store.patch_tokens("kb", {"expires_at": None}, where={"version": [raw_generation(row, "version")]})
    clock.advance(days=30)
    access, _ = await _tokens(store)
    assert await svc.access_token(target) == access and _refreshes(mock_oauth_mcp) == 0


async def test_a_browser_token_without_a_refresh_token_serves_until_it_expires(svc, kb, target, store, mock_oauth_mcp, clock):
    mock_oauth_mcp.state.issue_refresh_tokens = False
    await connect(svc, kb)
    access, refresh = await _tokens(store)
    assert refresh is None
    _near_expiry(clock)
    assert await svc.access_token(target) == access, "nothing can refresh it, so it serves its last 30 seconds"
    clock.advance(seconds=31)
    with pytest.raises(McpSignInRequiredError) as caught:
        await svc.access_token(target)
    assert caught.value.reason == EXPIRED_NO_REFRESH
    row = await store.get_tokens("kb")
    assert (row.state, row.reason, row.access_token_enc) == ("needs_sign_in", EXPIRED_NO_REFRESH, None)
    assert mock_oauth_mcp.state.token_requests[-1]["grant_type"] == "authorization_code", "no refresh was attempted"


# ---- failures ------------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_a_refused_refresh_signs_the_toolset_out(svc, kb, target, store, mock_oauth_mcp, clock):
    await connect(svc, kb)
    mock_oauth_mcp.state.revoke_all()
    _near_expiry(clock)
    with pytest.raises(McpSignInRequiredError) as caught:
        await svc.access_token(target)
    reason = "The authorization server refused the refresh token (invalid_grant)."
    assert str(caught.value) == "the MCP server kb needs an operator to reconnect it" and caught.value.reason == reason
    row = await store.get_tokens("kb")
    assert (row.state, row.reason, row.access_token_enc, row.refresh_token_enc, row.refresh_lease_until) == ("needs_sign_in", reason, None, None, None)
    status = await svc.status(kb)
    assert (status.state, status.reason) == ("needs_sign_in", reason)
    requests = len(mock_oauth_mcp.state.token_requests)
    with pytest.raises(McpSignInRequiredError):
        await svc.access_token(target)
    assert len(mock_oauth_mcp.state.token_requests) == requests, "a signed-out toolset makes no request"


async def test_a_token_endpoint_outage_keeps_the_refresh_token(svc, kb, target, store, mock_oauth_mcp, clock):
    """Review Focus 4: the refresh token is kept, the status is Error with the reason, the call fails with a temporary error, and the next call after
    the recovery works without a reconnect."""
    await connect(svc, kb)
    old_access, old_refresh = await _tokens(store)
    _near_expiry(clock)
    mock_oauth_mcp.state.token_status_override = 503
    with pytest.raises(ProviderError, match="^the MCP server kb could not refresh its sign-in") as caught:
        await svc.access_token(target)
    assert not isinstance(caught.value, McpSignInRequiredError)
    row = await store.get_tokens("kb")
    assert row.state == "error" and store.unseal(row.refresh_token_enc) == old_refresh and row.refresh_lease_until is None
    assert row.reason.startswith("The authorization server could not be reached: token endpoint answered 503")
    assert (await svc.status(kb)).model_dump(include={"state", "reason"}) == {"state": "error", "reason": row.reason}
    mock_oauth_mcp.state.token_status_override = None
    recovered = await svc.access_token(target)
    assert recovered != old_access
    row = await store.get_tokens("kb")
    assert (row.state, row.reason) == ("connected", None) and store.unseal(row.refresh_token_enc) != old_refresh


async def test_an_unreachable_token_endpoint_keeps_the_refresh_token(svc, kb, target, store, clock):
    await connect(svc, kb)
    _, old_refresh = await _tokens(store)
    row = await store.get_tokens("kb")
    await store.patch_tokens("kb", {"token_endpoint": "http://127.0.0.1:9/token"}, where={"version": [raw_generation(row, "version")]})
    _near_expiry(clock)
    with pytest.raises(ProviderError, match=r"could not be reached: the token endpoint did not answer \(ConnectError\)"):
        await svc.access_token(target)
    row = await store.get_tokens("kb")
    assert row.state == "error" and store.unseal(row.refresh_token_enc) == old_refresh and row.refresh_lease_owner is None


async def test_a_token_endpoint_that_hangs_is_bounded_well_inside_the_lease(svc, kb, target, store, mock_oauth_mcp, clock, monkeypatch):
    monkeypatch.setattr(service_module, "TOKEN_CALL_TIMEOUT_S", 0.2)
    assert service_module.HTTP_TIMEOUT_S * 3 <= LEASE_TTL.total_seconds(), "the lease outlives a whole token call three times over"
    await connect(svc, kb)
    _, old_refresh = await _tokens(store)
    _near_expiry(clock)
    mock_oauth_mcp.state.token_delay_s = 3.0
    with pytest.raises(ProviderError, match=r"did not answer within 0\.2s"):
        async with asyncio.timeout(_BOUND_S):
            await svc.access_token(target)
    row = await store.get_tokens("kb")
    assert row.state == "error" and store.unseal(row.refresh_token_enc) == old_refresh and row.refresh_lease_until is None


async def test_a_changed_url_never_gets_the_old_token(svc, kb, store, mock_oauth_mcp):
    await connect(svc, kb)
    seen, requests = len(mock_oauth_mcp.state.seen_tokens), len(mock_oauth_mcp.state.token_requests)
    moved = target_of(make_toolset(mock_oauth_mcp.base_url + "/other-mcp", oauth={}))
    for call in (svc.access_token(moved), svc.refresh_after_401(moved, rejected="x", www_authenticate=None)):
        with pytest.raises(McpSignInRequiredError) as caught:
            await call
        assert caught.value.reason == SETTINGS_CHANGED
    assert len(mock_oauth_mcp.state.token_requests) == requests and len(mock_oauth_mcp.state.seen_tokens) == seen


def _says_the_key_differs(caplog, toolset_id: str = "kb") -> bool:
    return any(
        record.levelno == logging.ERROR and toolset_id in record.getMessage() and "encryption key" in record.getMessage()
        for record in caplog.records
    )


async def test_a_process_that_cannot_read_the_sign_in_fails_only_its_own_call(services, kb, target, store, caplog):
    """Lead ruling on audit G4-10 (this replaces the test that pinned the old design, which wiped the shared tokens): a process whose key cannot open
    the token row fails its own call (needs sign-in, KEY_CHANGED) and logs an ERROR that names the toolset and the key. The row is left as it is, so
    the processes that can open it keep using it; only an admin Disconnect or Reconnect replaces it."""
    pod_a = services("pod-a", secret_box=SecretBox.from_key_material(OTHER_KEY))
    await connect(pod_a, kb)
    before = await store.get_tokens("kb")
    caplog.set_level(logging.ERROR, logger=service_module.__name__)
    for call in (services("pod-b").access_token(target), services("pod-b").refresh_after_401(target, rejected="x", www_authenticate=None)):
        with pytest.raises(McpSignInRequiredError) as caught:
            await call
        assert caught.value.reason == KEY_CHANGED
    assert _says_the_key_differs(caplog)
    after = await store.get_tokens("kb")
    assert after.model_dump() == before.model_dump(), "the process that cannot read the row changed it"
    assert after.access_token_enc is not None and after.refresh_token_enc is not None and after.state == "connected"
    assert await pod_a.access_token(target) == pod_a.store.unseal(after.access_token_enc), "the process that can read it lost its sign-in"


async def test_a_refresh_that_cannot_read_the_refresh_token_keeps_the_shared_tokens(svc, kb, target, store, clock, caplog, mock_oauth_mcp):
    """The same ruling on the refresh path: a row whose access token this process reads but whose refresh token another key sealed. The lease the
    process took is released, nothing else is written, and the call fails as needs sign-in (KEY_CHANGED)."""
    await connect(svc, kb)
    row = await store.get_tokens("kb")
    sealed_elsewhere = SecretStr(SecretBox.from_key_material(OTHER_KEY).encrypt("rt-of-another-key"))
    assert await store.patch_tokens("kb", {"refresh_token_enc": sealed_elsewhere}, where={"version": [raw_generation(row, "version")]})
    before = await store.get_tokens("kb")
    _near_expiry(clock)
    requests = len(mock_oauth_mcp.state.token_requests)
    caplog.set_level(logging.ERROR, logger=service_module.__name__)
    with pytest.raises(McpSignInRequiredError) as caught:
        async with asyncio.timeout(_BOUND_S):
            await svc.access_token(target)
    assert caught.value.reason == KEY_CHANGED and _says_the_key_differs(caplog)
    after = await store.get_tokens("kb")
    assert (after.refresh_lease_until, after.refresh_lease_owner) == (None, None), "the lease was not released"
    assert after.model_dump() == before.model_dump(), "the shared tokens were changed"
    assert len(mock_oauth_mcp.state.token_requests) == requests, "a refresh was attempted without a readable refresh token"


# ---- after a 401 ---------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_refresh_after_401_returns_a_token_another_pod_stored(services, kb, target, store, mock_oauth_mcp, clock):
    a, b = services("pod-a"), services("pod-b")
    await connect(a, kb)
    old, _ = await _tokens(store)
    _near_expiry(clock)
    new = await b.access_token(target)
    requests = len(mock_oauth_mcp.state.token_requests)
    assert await a.refresh_after_401(target, rejected=old, www_authenticate='Bearer error="invalid_token"') == new
    assert len(mock_oauth_mcp.state.token_requests) == requests


async def test_refresh_after_401_forces_a_refresh(svc, kb, target, store, mock_oauth_mcp):
    await connect(svc, kb)
    old_access, old_refresh = await _tokens(store)
    new = await svc.refresh_after_401(target, rejected=old_access, www_authenticate='Bearer error="invalid_token"')
    assert new != old_access and _refreshes(mock_oauth_mcp) == 1, "the token looked fresh by its expiry, and was refreshed anyway"
    row = await store.get_tokens("kb")
    assert row.version == 2 and store.unseal(row.refresh_token_enc) != old_refresh and row.state == "connected"


async def test_refresh_after_401_without_a_refresh_token_signs_out(svc, kb, target, store, mock_oauth_mcp):
    mock_oauth_mcp.state.issue_refresh_tokens = False
    await connect(svc, kb)
    access, _ = await _tokens(store)
    with pytest.raises(McpSignInRequiredError) as caught:
        await svc.refresh_after_401(target, rejected=access, www_authenticate=None)
    assert caught.value.reason == TOKEN_REJECTED == "The MCP server rejected the access token."
    row = await store.get_tokens("kb")
    assert (row.state, row.reason, row.access_token_enc) == ("needs_sign_in", TOKEN_REJECTED, None)


# ---- client credentials --------------------------------------------------------------------------------------------------------------------------------------------------


async def test_client_credentials_tokens_are_fetched_again_on_expiry(svc, store, mock_oauth_mcp, clock):
    target = target_of(make_toolset(mock_oauth_mcp.mcp_url, oauth=CC))
    await svc.connect_client_credentials(target, connected_by="admin")
    old, _ = await _tokens(store)
    _near_expiry(clock)
    new = await svc.access_token(target)
    assert new != old and [r["grant_type"] for r in mock_oauth_mcp.state.token_requests] == ["client_credentials", "client_credentials"]
    row = await store.get_tokens("kb")
    assert (row.version, row.refresh_token_enc, row.state) == (2, None, "connected")
    assert mcp_answers(mock_oauth_mcp.mcp_url, new) == 200


async def test_refused_client_credentials_are_an_error_that_a_new_secret_recovers(svc, store, mock_oauth_mcp, clock):
    target = target_of(make_toolset(mock_oauth_mcp.mcp_url, oauth=CC))
    await svc.connect_client_credentials(target, connected_by="admin")
    mock_oauth_mcp.state.static_clients["cc-client"] = "rotated-secret"
    _near_expiry(clock)
    with pytest.raises(McpSignInRequiredError) as caught:
        await svc.access_token(target)
    reason = "The authorization server refused the client credentials (invalid_client)."
    assert caught.value.reason == reason
    row = await store.get_tokens("kb")
    assert (row.state, row.reason, row.access_token_enc) == ("error", reason, None)
    fixed = target_of(make_toolset(mock_oauth_mcp.mcp_url, oauth={**CC, "static_client": {"client_id": "cc-client", "client_secret": "rotated-secret"}}))
    assert config_fingerprint(fixed) == row.config_fingerprint, "a new secret for the same client keeps the sign-in"
    token = await svc.access_token(fixed)
    row = await store.get_tokens("kb")
    assert row.state == "connected" and store.unseal(row.access_token_enc) == token


# ---- a refresh meets a cancellation, a disconnect or a reconnect ---------------------------------------------------------------------------------------------------------


async def test_a_cancelled_refresh_still_stores_the_rotated_token(svc, kb, target, store, mock_oauth_mcp, clock):
    """A Stop during a refresh must not lose a rotated refresh token: the caller is cancelled at once, the exchange runs to its end and is stored."""
    await connect(svc, kb)
    _, old_refresh = await _tokens(store)
    _near_expiry(clock)
    mock_oauth_mcp.state.token_delay_s = 0.5
    task = asyncio.create_task(svc.access_token(target))
    await _refresh_request_arrived(mock_oauth_mcp)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def stored() -> bool:
        return (await store.get_tokens("kb")).version == 2

    await _until(stored, what="the rotated tokens")
    row = await store.get_tokens("kb")
    assert row.state == "connected" and row.refresh_lease_until is None and store.unseal(row.refresh_token_enc) != old_refresh
    requests = len(mock_oauth_mcp.state.token_requests)
    assert await svc.access_token(target) == store.unseal(row.access_token_enc) and len(mock_oauth_mcp.state.token_requests) == requests


async def test_a_disconnect_during_a_refresh_is_not_undone(svc, kb, target, store, mock_oauth_mcp, clock):
    await connect(svc, kb)
    _near_expiry(clock)
    mock_oauth_mcp.state.token_delay_s = 0.5
    task = asyncio.create_task(svc.access_token(target))
    await _refresh_request_arrived(mock_oauth_mcp)
    await store.delete_tokens("kb")                    # an admin disconnects, on another server process
    with pytest.raises(McpSignInRequiredError) as caught:
        async with asyncio.timeout(_BOUND_S):
            await task
    assert caught.value.reason == NOT_CONNECTED and await store.get_tokens("kb") is None


async def test_a_reconnect_during_a_refresh_wins(svc, kb, target, store, mock_oauth_mcp, clock):
    await connect(svc, kb)
    _near_expiry(clock)
    mock_oauth_mcp.state.token_delay_s = 0.5
    task = asyncio.create_task(svc.access_token(target))
    await _refresh_request_arrived(mock_oauth_mcp)
    reconnected = token_row(store, target, access_token_enc=store.seal("at-reconnected"), refresh_token_enc=store.seal("rt-reconnected"),
                            expires_at=clock.now + timedelta(hours=1))
    await store.replace_tokens(reconnected)            # an admin reconnects, on another server process
    async with asyncio.timeout(_BOUND_S):
        assert await task == "at-reconnected"
    row = await store.get_tokens("kb")
    assert (store.unseal(row.access_token_enc), store.unseal(row.refresh_token_enc), row.version) == ("at-reconnected", "rt-reconnected", 2)


# ---- nothing secret reaches the log --------------------------------------------------------------------------------------------------------------------------------------


async def test_refresh_paths_never_log_a_token(caplog, svc, kb, target, store, mock_oauth_mcp, clock):
    caplog.set_level(logging.DEBUG)
    await connect(svc, kb)
    seen = list(await _tokens(store))
    _near_expiry(clock)
    seen.append(await svc.access_token(target))
    seen.append((await _tokens(store))[1])
    clock.advance(minutes=59, seconds=40)
    mock_oauth_mcp.state.token_status_override = 503
    with pytest.raises(ProviderError):
        await svc.access_token(target)
    mock_oauth_mcp.state.token_status_override = None
    mock_oauth_mcp.state.revoke_all()
    with pytest.raises(McpSignInRequiredError):
        await svc.access_token(target)
    assert any(record.name.startswith("primer.toolset.oauth") and record.levelno >= logging.WARNING for record in caplog.records)
    for secret in seen:
        assert secret and secret not in caplog.text
