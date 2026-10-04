"""tests/llm must never reach the real network.

These are unit tests of the provider adapters: every request is meant to be
intercepted in-process. The failure this guards against is a mock that
silently misses. That is exactly what happened to test_openrouter.py: the
openai SDK moved to ``httpx2``, respx (which patches ``httpx`` only) stopped
intercepting, and the tests sent real requests to openrouter.ai. They were
excluded from CI, so nothing noticed, and a miss looked like a credentials
error (a real 401) instead of a missing mock.

The adapters wrap transport failures into their own error types, so a
blocked connection could be swallowed and surface as an unrelated
NetworkError. The guard therefore records every attempt and fails the test
at teardown naming the host, so it stays loud however the code under test
handles the error.
"""

from __future__ import annotations

import socket

import pytest

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _is_local(host: object) -> bool:
    if not isinstance(host, str):
        return True
    return (
        host in _LOOPBACK_NAMES
        or host == "::1"
        or host.startswith("127.")
        or host == ""
    )


class NetworkBlocked(OSError):
    """Raised when a tests/llm test tries to leave the machine."""


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch):
    attempted: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _blocked(host: object) -> NetworkBlocked:
        attempted.append(str(host))
        return NetworkBlocked(
            f"tests/llm tried to reach {host!r}: a mock missed. Intercept the "
            "SDK's own transport (see tests/llm/test_openrouter.py::wire)."
        )

    def connect(self, address):
        if isinstance(address, tuple) and not _is_local(address[0]):
            raise _blocked(address[0])
        return real_connect(self, address)

    def connect_ex(self, address):
        if isinstance(address, tuple) and not _is_local(address[0]):
            raise _blocked(address[0])
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            raise _blocked(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield attempted
    assert not attempted, (
        f"tests/llm reached the real network: {sorted(set(attempted))}"
    )
