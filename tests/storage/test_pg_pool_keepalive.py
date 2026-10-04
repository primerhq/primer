"""TCP keepalive for the asyncpg pools built from PoolConfig.

An idle LISTEN connection sends no packets, so when its peer vanishes without a
FIN or RST (a conntrack or NAT timeout, a partition) nothing ever notices: the
socket never errors, asyncpg's termination listener never fires, and the
watcher parks on a dead connection for good. Kernel keepalive turns that into an
ordinary connection loss within ``idle + interval * count`` seconds, and its
probes also keep a 24h conntrack entry alive.

asyncpg exposes no keepalive option, so the pool's ``init`` hook sets the socket
options through ``Connection._transport``. These tests drive that hook with a
fake socket: the options it sets, and every way it must degrade (a platform
without the options, a changed asyncpg, a failing setsockopt) without ever
failing pool creation and while warning ONCE per pool. The real-socket
read-back is in test_postgres_tcp_keepalive_live.py.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import socket
from types import SimpleNamespace

import asyncpg
import pytest
from pydantic import ValidationError

from primer.model.provider import PoolConfig
from primer.storage._pg_pool import keepalive_init_hook

# macOS spells the idle option TCP_KEEPALIVE; Linux has TCP_KEEPIDLE.
_IDLE_OPT = getattr(socket, "TCP_KEEPIDLE", None) or socket.TCP_KEEPALIVE
_LOGGER = "primer.storage._pg_pool"


class _Sock:
    def __init__(
        self,
        *,
        family=socket.AF_INET,
        fail: Exception | None = None,
        fail_option: int | None = None,
    ) -> None:
        self.family = family
        self._fail = fail
        self._fail_option = fail_option  # fail only when this option is set (None: fail every call)
        self.calls: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        if self._fail is not None and self._fail_option in (None, option):
            raise self._fail
        self.calls.append((level, option, value))


def _conn(sock) -> SimpleNamespace:
    transport = SimpleNamespace(get_extra_info=lambda name: sock if name == "socket" else None)
    return SimpleNamespace(_transport=transport)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]


# ---- the config model ------------------------------------------------------


def test_the_defaults_detect_a_vanished_peer_within_ninety_seconds():
    cfg = PoolConfig()

    assert (cfg.tcp_keepalive_idle_seconds, cfg.tcp_keepalive_interval_seconds,
            cfg.tcp_keepalive_count) == (60, 10, 3)
    assert cfg.tcp_keepalive_idle_seconds + cfg.tcp_keepalive_interval_seconds * cfg.tcp_keepalive_count == 90


def test_an_idle_of_zero_is_valid_and_means_off():
    assert PoolConfig(tcp_keepalive_idle_seconds=0).tcp_keepalive_idle_seconds == 0


@pytest.mark.parametrize(
    "field, value",
    [
        ("tcp_keepalive_idle_seconds", -1),
        ("tcp_keepalive_interval_seconds", 0),
        ("tcp_keepalive_count", 0),
    ],
)
def test_nonsensical_timings_are_rejected(field, value):
    with pytest.raises(ValidationError):
        PoolConfig(**{field: value})


@pytest.mark.parametrize(
    "field, limit",
    [
        ("tcp_keepalive_idle_seconds", 32767),
        ("tcp_keepalive_interval_seconds", 32767),
        ("tcp_keepalive_count", 127),
    ],
)
def test_the_timings_are_bounded_by_the_linux_limits(field, limit):
    """An out-of-range value used to pass validation, apply partially, and then
    be reported as "keepalive is off" while SO_KEEPALIVE was in fact on."""
    assert getattr(PoolConfig(**{field: limit}), field) == limit  # the limit itself is valid
    with pytest.raises(ValidationError):
        PoolConfig(**{field: limit + 1})


def test_a_config_saved_before_these_fields_existed_still_loads():
    cfg = PoolConfig.model_validate(
        {"min_size": 1, "max_size": 4, "acquire_timeout": 30.0, "max_idle": 300.0, "max_lifetime": 3600.0}
    )

    assert cfg.tcp_keepalive_idle_seconds == 60


# ---- the hook --------------------------------------------------------------


async def test_it_sets_keepalive_with_the_configured_timings():
    cfg = PoolConfig(
        tcp_keepalive_idle_seconds=45, tcp_keepalive_interval_seconds=7, tcp_keepalive_count=4,
    )
    hook = keepalive_init_hook(cfg, pool_name="storage")
    sock = _Sock()

    await hook(_conn(sock))

    # SO_KEEPALIVE last: keepalive is only switched on once its timings are in place.
    assert sock.calls == [
        (socket.IPPROTO_TCP, _IDLE_OPT, 45),
        (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 7),
        (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4),
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]


@pytest.mark.parametrize(
    "failing, name",
    [(_IDLE_OPT, "TCP_KEEPIDLE"), (socket.TCP_KEEPINTVL, "TCP_KEEPINTVL"),
     (socket.TCP_KEEPCNT, "TCP_KEEPCNT"), (socket.SO_KEEPALIVE, "SO_KEEPALIVE")],
)
async def test_a_failing_option_is_named_and_leaves_keepalive_off(failing, name, caplog):
    """One option failing must not leave keepalive switched on with the kernel's
    default timings while the warning says it is off, and the warning must say
    WHICH option failed."""
    hook = keepalive_init_hook(PoolConfig(), pool_name="storage")
    sock = _Sock(fail=OSError("invalid argument"), fail_option=failing)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        await hook(_conn(sock))

    (warning,) = _warnings(caplog)
    assert name in warning.getMessage()
    assert "NOT enabled" in warning.getMessage()
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) not in sock.calls, (
        "SO_KEEPALIVE was switched on although a timing failed: keepalive would run on kernel defaults"
    )


async def test_the_options_land_on_a_real_tcp_socket():
    """Runs in CI (no database): a real asyncio TCP connection, the real hook,
    and the options read back with getsockopt."""
    # Keep the server side of the connection so it can be closed in the finally:
    # left open it is garbage-collected later, and its ResourceWarning fires
    # against whichever unrelated test happens to be running then.
    server_side: list[asyncio.StreamWriter] = []

    async def accept(_reader, accepted_writer):
        server_side.append(accepted_writer)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        hook = keepalive_init_hook(
            PoolConfig(tcp_keepalive_idle_seconds=45, tcp_keepalive_interval_seconds=7, tcp_keepalive_count=4),
            pool_name="storage",
        )
        await hook(SimpleNamespace(_transport=writer.transport))

        sock = writer.transport.get_extra_info("socket")
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        assert sock.getsockopt(socket.IPPROTO_TCP, _IDLE_OPT) == 45
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 7
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 4
    finally:
        for _ in range(100):  # the accept callback runs as its own task
            if server_side:
                break
            await asyncio.sleep(0.01)
        writer.close()
        for accepted in server_side:
            accepted.close()
        await writer.wait_closed()
        for accepted in server_side:
            await accepted.wait_closed()
        server.close()
        await server.wait_closed()


def test_asyncpg_still_exposes_the_private_transport_the_hook_reaches_through():
    """The hook reads the socket from ``Connection._transport``, a private
    attribute. This is the check CI runs without a database (the live test reads
    the options back from a real pooled connection): an asyncpg upgrade that
    renames or drops it fails here instead of silently turning keepalive off."""
    assert "_transport" in asyncpg.connection.Connection.__slots__


def test_the_hook_is_a_coroutine_function_as_asyncpg_requires():
    hook = keepalive_init_hook(PoolConfig(), pool_name="storage")

    assert inspect.iscoroutinefunction(hook)


def test_an_idle_of_zero_installs_no_hook_and_says_nothing(caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        hook = keepalive_init_hook(PoolConfig(tcp_keepalive_idle_seconds=0), pool_name="storage")

    assert hook is None
    assert _warnings(caplog) == []


async def test_a_unix_socket_is_left_alone(caplog):
    """TCP options do not apply to a unix-socket connection, and that is not worth a warning."""
    hook = keepalive_init_hook(PoolConfig(), pool_name="storage")
    sock = _Sock(family=socket.AF_UNIX)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        await hook(_conn(sock))

    assert sock.calls == []
    assert _warnings(caplog) == []


class _RaisingTransport:
    def get_extra_info(self, name):
        raise RuntimeError("transport is gone")


@pytest.mark.parametrize(
    "make_conn, cause",
    [
        pytest.param(lambda: SimpleNamespace(), "_transport", id="asyncpg-without-_transport"),
        pytest.param(lambda: _conn(None), "socket", id="no-socket-on-the-transport"),
        pytest.param(lambda: _conn(_Sock(fail=OSError("not supported"))), "not supported", id="setsockopt-fails"),
        pytest.param(lambda: SimpleNamespace(_transport=_RaisingTransport()), "transport is gone", id="transport-raises"),
    ],
)
async def test_a_connection_it_cannot_configure_never_fails_and_warns_once(make_conn, cause, caplog):
    hook = keepalive_init_hook(PoolConfig(), pool_name="storage")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        for _ in range(5):
            await hook(make_conn())  # must not raise: an init hook that raises fails pool creation

    (warning,) = _warnings(caplog)  # ONE warning for the pool, not one per connection
    assert "storage" in warning.getMessage()
    assert cause in warning.getMessage()


async def test_each_pool_warns_once_for_itself(caplog):
    first = keepalive_init_hook(PoolConfig(), pool_name="storage")
    second = keepalive_init_hook(PoolConfig(), pool_name="pgvector")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        for hook in (first, second, first, second):
            await hook(SimpleNamespace())

    names = sorted(
        "storage" if "storage" in w.getMessage() else "pgvector" for w in _warnings(caplog)
    )
    assert names == ["pgvector", "storage"]


def _fake_socket_module(**overrides):
    base = dict(
        SOL_SOCKET=1, SO_KEEPALIVE=9, IPPROTO_TCP=6, AF_INET=2, AF_INET6=10,
        TCP_KEEPIDLE=4, TCP_KEEPINTVL=5, TCP_KEEPCNT=6,
    )
    base.update(overrides)
    return SimpleNamespace(**{k: v for k, v in base.items() if v is not None})


def test_a_platform_without_the_options_installs_no_hook_and_warns_once(caplog):
    """E.g. a Python build whose socket module has no TCP_KEEPINTVL."""
    sm = _fake_socket_module(TCP_KEEPINTVL=None)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        hook = keepalive_init_hook(PoolConfig(), pool_name="storage", socket_module=sm)

    assert hook is None
    (warning,) = _warnings(caplog)
    assert "TCP_KEEPINTVL" in warning.getMessage()
    assert "storage" in warning.getMessage()


async def test_macos_spells_the_idle_option_tcp_keepalive():
    sm = _fake_socket_module(TCP_KEEPIDLE=None, TCP_KEEPALIVE=0x10)
    hook = keepalive_init_hook(PoolConfig(), pool_name="storage", socket_module=sm)
    sock = _Sock(family=sm.AF_INET)

    await hook(_conn(sock))

    assert (sm.IPPROTO_TCP, 0x10, 60) in sock.calls
