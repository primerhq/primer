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

import inspect
import logging
import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from primer.model.provider import PoolConfig
from primer.storage._pg_pool import keepalive_init_hook

# macOS spells the idle option TCP_KEEPALIVE; Linux has TCP_KEEPIDLE.
_IDLE_OPT = getattr(socket, "TCP_KEEPIDLE", None) or socket.TCP_KEEPALIVE
_LOGGER = "primer.storage._pg_pool"


class _Sock:
    def __init__(self, *, family=socket.AF_INET, fail: Exception | None = None) -> None:
        self.family = family
        self._fail = fail
        self.calls: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        if self._fail is not None:
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

    assert sock.calls == [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.IPPROTO_TCP, _IDLE_OPT, 45),
        (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 7),
        (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4),
    ]


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
