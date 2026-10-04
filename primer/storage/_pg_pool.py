"""Tuning shared by every asyncpg pool built from :class:`PoolConfig`.

TCP keepalive
-------------
A pooled connection that sits idle sends no packets. A LISTEN connection is
idle almost all of its life (a worker holds several, for as long as it runs),
so if its peer vanishes without a FIN or RST (a conntrack or NAT idle timeout,
a network partition) the socket never errors, asyncpg's termination listener
never fires, and the watcher stays parked on a dead connection for good. Worse,
the connection that dies is the one a NOTIFY was in flight on, which can be a
user's cancel.

Kernel keepalive turns that into an ordinary connection loss within
``idle + interval * count`` seconds (90 with the defaults), and the probes also
keep a conntrack or NAT entry alive (kube-proxy expires idle ones after 24
hours), which prevents the likeliest trigger in the first place.

asyncpg has no keepalive option (the only socket option it sets is
TCP_NODELAY), so :func:`keepalive_init_hook` returns a pool ``init`` hook that
reaches the socket through ``Connection._transport``. That is a private
attribute, so a live-Postgres test reads the options back from a pooled
connection and fails loudly if an asyncpg upgrade moves it. Whatever goes wrong
here, the hook never raises: an exception from ``init`` would fail pool
creation, and a missing keepalive is only worth one warning per pool.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from primer.model.providers.storage import PoolConfig

logger = logging.getLogger(__name__)

InitHook = Callable[[Any], Awaitable[None]]


def warn_unenforced_pool_options(pool_cfg: PoolConfig, *, pool_name: str) -> None:
    """Warn once, at pool creation, about a PoolConfig option that does nothing.

    ``max_lifetime`` was documented as recycling connections after an age
    (defending against leaks) and nothing ever read it: asyncpg has no maximum
    connection age, and an age limit could not reach the LISTEN connections a
    worker holds checked out for its whole life anyway. The field is kept so a
    saved config that sets it still validates; a non-default value, which means
    somebody is relying on it, gets this warning.
    """
    default = PoolConfig.model_fields["max_lifetime"].default
    if pool_cfg.max_lifetime != default:
        logger.warning(
            "%s pool: max_lifetime=%s is set but NOT enforced and has no effect (asyncpg cannot "
            "recycle connections by age, and an age limit would never reach the LISTEN connections "
            "a worker holds checked out); it is accepted only so existing configs keep validating",
            pool_name, pool_cfg.max_lifetime,
        )


def keepalive_init_hook(
    pool_cfg: PoolConfig,
    *,
    pool_name: str,
    socket_module: Any = socket,
) -> InitHook | None:
    """The asyncpg pool ``init`` hook that turns TCP keepalive on, or ``None``.

    ``None`` when ``tcp_keepalive_idle_seconds`` is 0 (switched off), or when
    this platform's socket module lacks an option we need (one warning, naming
    it). ``socket_module`` is only a seam for tests.
    """
    idle = pool_cfg.tcp_keepalive_idle_seconds
    if idle == 0:
        return None
    interval = pool_cfg.tcp_keepalive_interval_seconds
    count = pool_cfg.tcp_keepalive_count

    sm = socket_module
    idle_opt = getattr(sm, "TCP_KEEPIDLE", None)
    if idle_opt is None:
        idle_opt = getattr(sm, "TCP_KEEPALIVE", None)  # macOS spells the idle option this way
    required = {
        "SO_KEEPALIVE": getattr(sm, "SO_KEEPALIVE", None),
        "TCP_KEEPIDLE": idle_opt,
        "TCP_KEEPINTVL": getattr(sm, "TCP_KEEPINTVL", None),
        "TCP_KEEPCNT": getattr(sm, "TCP_KEEPCNT", None),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        logger.warning(
            "%s pool: TCP keepalive is configured (tcp_keepalive_idle_seconds=%s) but this "
            "platform's socket module has no %s, so connections on this pool will not detect "
            "a peer that vanished without a FIN or RST",
            pool_name, idle, ", ".join(missing),
        )
        return None

    tcp_families = tuple(
        family for family in (getattr(sm, "AF_INET", None), getattr(sm, "AF_INET6", None))
        if family is not None
    )
    # SO_KEEPALIVE goes LAST. Switching it on first and then failing on a timing
    # would leave keepalive on with the kernel's default timings (2 hours idle),
    # while the warning below says it is not enabled. This order makes that
    # statement true: keepalive is only switched on once its timings are in place.
    options = (
        ("TCP_KEEPIDLE", sm.IPPROTO_TCP, idle_opt, idle),
        ("TCP_KEEPINTVL", sm.IPPROTO_TCP, sm.TCP_KEEPINTVL, interval),
        ("TCP_KEEPCNT", sm.IPPROTO_TCP, sm.TCP_KEEPCNT, count),
        ("SO_KEEPALIVE", sm.SOL_SOCKET, sm.SO_KEEPALIVE, 1),
    )
    warned = False

    def _warn_once(reason: str) -> None:
        nonlocal warned
        if warned:
            return
        warned = True
        logger.warning(
            "%s pool: could not enable TCP keepalive on a connection (%s), so it is NOT enabled "
            "there and this pool may not detect a peer that vanished without a FIN or RST; set "
            "tcp_keepalive_idle_seconds=0 to silence this",
            pool_name, reason,
        )

    async def _init(conn: Any) -> None:
        step = "reading the connection's socket"
        try:
            sock = conn._transport.get_extra_info("socket")
            if sock is None:
                _warn_once("the connection's transport exposes no socket")
                return
            if sock.family not in tcp_families:
                return  # a unix-socket connection: TCP options do not apply to it
            for name, level, option, value in options:
                step = f"setting {name}"
                sock.setsockopt(level, option, value)
        except Exception as exc:  # noqa: BLE001 - an init hook that raises fails pool creation
            _warn_once(f"{step}: {type(exc).__name__}: {exc}")

    return _init
