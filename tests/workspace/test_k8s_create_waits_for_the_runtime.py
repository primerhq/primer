"""The k8s backend waits for the runtime in a Running pod to start listening (ticket 01a11543).

A pod is ``Running`` as soon as its container has STARTED, which is before the runtime process listens. ``create`` and
``_reattach`` waited for the pod phase and then connected ONCE, so the first real ``materialise`` on k3s died with
``connect refused :5959`` and the next attempt (a few seconds later) worked. The Docker adapter does not have the race: it polls
the runtime's ready marker before it connects. Here the connect itself is retried, with a FRESH client per attempt (a failed
``connect`` leaves its aiohttp session, and after a hello timeout maybe a half-open socket, behind) and a bound.

What is retried is only "the runtime is not serving yet": a refused or dropped connection, a hello that timed out, and the
gateway answering 404/502/503/504 for a backend it has not programmed or reached yet. Anything else (a 401/403, a protocol
error) fails at once, on the first attempt, with its own error.
"""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from primer.model.except_ import ConfigError
from primer.workspace.k8s import backend as k8s_backend
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.workspace.test_k8s_client_is_closed_when_the_build_does_not_finish import TEMPLATE, _backend, _create, _reattach

BUILDS = pytest.mark.parametrize("build", [_reattach, _create], ids=["reattach", "create"])

# A build that waits for a runtime must never be able to loop forever in a test: without a deadline in the code under test, a
# test that waits for it would HANG the lane (pytest's per-test timeout does not interrupt an event loop that keeps sleeping).
# Every direct build goes through this bound, so a missing deadline FAILS (a TimeoutError, not the ConfigError the test expects).
HARD_BOUND_S = 5.0


async def _bounded(build, backend):
    async with asyncio.timeout(HARD_BOUND_S):
        return await build(backend)

REFUSED = ConnectionRefusedError(111, "Connect call failed ('10.42.0.7', 5959)")


_KEY = aiohttp.client_reqrep.ConnectionKey("10.42.0.7", 5959, True, None, None, None, None)


def _tls_error() -> aiohttp.ClientSSLError:
    return aiohttp.ClientSSLError(_KEY, ssl.SSLError("handshake failure"))


def _certificate_error() -> aiohttp.ClientConnectorCertificateError:
    return aiohttp.ClientConnectorCertificateError(_KEY, ssl.SSLCertVerificationError("certificate verify failed"))


def _handshake(status: int) -> aiohttp.WSServerHandshakeError:
    return aiohttp.WSServerHandshakeError(
        SimpleNamespace(real_url="ws://runtime.example/ws"), (), status=status, message=f"{status} from the gateway",
    )


class _Attempt:
    """One client per attempt; ``fail`` is what its ``connect`` raises (None: it connects)."""

    def __init__(self, fail: BaseException | None) -> None:
        self.fail = fail
        self.connected = False
        self.closed = 0

    async def connect(self) -> None:
        if self.fail is not None:
            raise self.fail
        self.connected = True

    async def aclose(self) -> None:
        self.closed += 1


class _Script:
    """Hands out the scripted clients in order and remembers every one of them."""

    def __init__(self, *fails: BaseException | None) -> None:
        self.attempts = [_Attempt(f) for f in fails]
        self.made: list[_Attempt] = []

    def __call__(self, **kwargs) -> _Attempt:
        client = self.attempts[len(self.made)] if len(self.made) < len(self.attempts) else _Attempt(self.attempts[-1].fail)
        self.made.append(client)
        return client


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    monkeypatch.setattr(k8s_backend, "_RUNTIME_READY_POLL_S", 0.01)
    monkeypatch.setattr(k8s_backend, "_RUNTIME_READY_MAX_POLL_S", 0.02)


@pytest.fixture(autouse=True)
def _wrapped_workspace(monkeypatch):
    async def wrap(**kwargs):
        return SimpleNamespace(id=kwargs["workspace_id"])

    monkeypatch.setattr(SandboxWorkspace, "materialise", staticmethod(wrap))


def _use(monkeypatch, script: _Script) -> _Script:
    monkeypatch.setattr(k8s_backend, "RuntimeClient", script)
    return script


# ---- the race itself ----------------------------------------------------------------------------------------------------------


@BUILDS
async def test_a_refused_connect_is_retried_until_the_runtime_listens(monkeypatch, build) -> None:
    script = _use(monkeypatch, _Script(REFUSED, REFUSED, None))
    backend = _backend()

    ws = await _bounded(build, backend)

    assert ws.id == "ws-1" and backend._workspaces == {"ws-1": ws}
    assert len(script.made) == 3
    assert [c.closed for c in script.made] == [1, 1, 0], "each failed attempt's client is closed, the connected one is the cache's"
    assert script.made[2].connected


@BUILDS
@pytest.mark.parametrize("status", [404, 502, 503, 504])
async def test_a_handshake_the_gateway_has_not_routed_yet_is_retried(monkeypatch, build, status) -> None:
    script = _use(monkeypatch, _Script(_handshake(status), None))
    ws = await _bounded(build, _backend())
    assert ws.id == "ws-1" and len(script.made) == 2 and script.made[0].closed == 1


@BUILDS
async def test_a_hello_that_times_out_is_retried(monkeypatch, build) -> None:
    script = _use(monkeypatch, _Script(TimeoutError(), None))
    await _bounded(build, _backend())
    assert len(script.made) == 2 and script.made[0].closed == 1


@BUILDS
async def test_a_real_aiohttp_connector_error_is_retried(monkeypatch, build) -> None:
    """The exception the live run actually raised is an ``aiohttp.ClientConnectorError``, not a bare ``ConnectionRefusedError``."""
    key = aiohttp.client_reqrep.ConnectionKey("10.42.0.7", 5959, False, None, None, None, None)
    script = _use(monkeypatch, _Script(aiohttp.ClientConnectorError(key, REFUSED), None))
    await _bounded(build, _backend())
    assert len(script.made) == 2


@BUILDS
async def test_a_runtime_that_is_already_up_is_connected_once_without_waiting(monkeypatch, build) -> None:
    """Proved structurally, not by the clock: a build that waits has to go through ``asyncio.sleep`` (the pause between
    attempts, or the pod-phase poll), so a recorder on it that stays empty means the build never entered a wait."""
    script = _use(monkeypatch, _Script(None))
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def recording_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(k8s_backend.asyncio, "sleep", recording_sleep)
    await _bounded(build, _backend())
    assert len(script.made) == 1 and sleeps == [], f"the build waited: sleeps {sleeps}"


# ---- what is NOT retried ------------------------------------------------------------------------------------------------------


@BUILDS
@pytest.mark.parametrize(
    "error",
    [
        _handshake(401), _handshake(403), RuntimeError("EPROTOCOL", "hello failed"), ValueError("not a connection problem"),
        _tls_error(), _certificate_error(),
    ],
    ids=["401", "403", "protocol", "other", "tls", "tls-certificate"],
)
async def test_an_error_that_is_not_the_runtime_starting_fails_at_once(monkeypatch, build, error) -> None:
    """The TLS cases matter most here: ``ClientSSLError`` and ``ClientConnectorCertificateError`` ARE ``ClientConnectionError``s,
    so only the classifier's explicit exclusion keeps a bad certificate from being retried for the whole 60 s. The script has a
    working second client on purpose: a classifier that retried would CONNECT on attempt 2 and the ``raises`` below would fail
    fast, not wait out the deadline."""
    script = _use(monkeypatch, _Script(error, None))
    backend = _backend()

    with pytest.raises(type(error)) as raised:
        await _bounded(build, backend)

    assert raised.value is error
    assert len(script.made) == 1 and script.made[0].closed == 1, "one attempt, and its client is closed"
    assert backend._workspaces == {}


# ---- the bound ----------------------------------------------------------------------------------------------------------------


@BUILDS
async def test_a_runtime_that_never_listens_fails_within_the_deadline_and_says_why(monkeypatch, build) -> None:
    monkeypatch.setattr(k8s_backend, "_RUNTIME_READY_TIMEOUT_S", 0.3)
    script = _use(monkeypatch, _Script(REFUSED))
    backend = _backend()

    # Boundedness is proved by ``_bounded``: an unbounded wait raises its TimeoutError, not the ConfigError expected here.
    with pytest.raises(ConfigError) as raised:
        await _bounded(build, backend)

    message = str(raised.value)
    assert "ws-1" in message and "did not accept a connection within 0.3s" in message
    assert "Connect call failed" in message and "5959" in message, "the last error must be in the message"
    assert "pod" in message.lower() and "logs" in message.lower(), "it says where to look"
    assert len(script.made) >= 2, "it did retry before giving up"
    assert all(c.closed == 1 and not c.connected for c in script.made), "every failed attempt's client is closed"
    assert backend._workspaces == {}


async def test_a_create_that_gives_up_rolls_back_the_cluster_objects_it_made(monkeypatch) -> None:
    monkeypatch.setattr(k8s_backend, "_RUNTIME_READY_TIMEOUT_S", 0.2)
    _use(monkeypatch, _Script(REFUSED))
    backend = _backend()
    backend._apps_v1.delete_namespaced_stateful_set = AsyncMock()
    backend._core_v1.delete_namespaced_service = AsyncMock()
    backend._core_v1.delete_namespaced_secret = AsyncMock()
    backend._core_v1.delete_namespaced_persistent_volume_claim = AsyncMock()

    with pytest.raises(ConfigError, match="did not accept a connection"):
        await _bounded(_create, backend)

    backend._apps_v1.delete_namespaced_stateful_set.assert_awaited()
    backend._core_v1.delete_namespaced_secret.assert_awaited()
    backend._core_v1.delete_namespaced_service.assert_awaited()


@BUILDS
async def test_a_cancel_while_waiting_between_attempts_closes_the_failed_client_and_propagates(monkeypatch, build) -> None:
    monkeypatch.setattr(k8s_backend, "_RUNTIME_READY_POLL_S", 5.0)       # park the build in its sleep between attempts
    monkeypatch.setattr(k8s_backend, "_RUNTIME_READY_MAX_POLL_S", 5.0)
    script = _use(monkeypatch, _Script(REFUSED))
    backend = _backend()
    task = asyncio.create_task(build(backend))
    for _ in range(200):
        if script.made and script.made[0].closed:
            break
        await asyncio.sleep(0.01)
    assert script.made and script.made[0].closed == 1, "the build did not reach its wait"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(script.made) == 1 and script.made[0].closed == 1, "no second client, and the first is not closed twice"
    assert backend._workspaces == {}


@BUILDS
async def test_a_cancel_during_an_attempt_closes_that_client_and_does_not_retry(monkeypatch, build) -> None:
    gate = asyncio.Event()

    class _Hangs(_Attempt):
        async def connect(self) -> None:
            self.entered = True
            await gate.wait()

    made: list[_Hangs] = []

    def factory(**kwargs):
        made.append(_Hangs(None))
        return made[-1]

    monkeypatch.setattr(k8s_backend, "RuntimeClient", factory)
    backend = _backend()
    task = asyncio.create_task(build(backend))
    for _ in range(200):
        if made and getattr(made[0], "entered", False):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(made) == 1 and made[0].closed == 1
