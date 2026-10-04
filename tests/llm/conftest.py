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
import tiktoken

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


@pytest.fixture(autouse=True)
def offline_tiktoken(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Serve tiktoken encodings from memory instead of loading real vocabularies.

    Counting normally goes through ``primer.llm._tokenizer._tiktoken_offline``,
    which reads a verified vocabulary file from the cache directory. Those
    files are ~5 MB, absent on a cold CI runner, and not what these tests are
    about. The seam ``_tiktoken_offline.load_encoding`` is therefore replaced
    by a real ``tiktoken.Encoding`` over a byte-level vocabulary: the real
    class and ``encode_ordinary`` contract, a deterministic synthetic
    vocabulary (one token per UTF-8 byte). What this keeps testing is primer's
    own logic: which encoding a model name maps to, how messages and tools are
    serialised, the per-message overhead, and that adapters route
    ``count_tokens`` through it. What it deliberately does not test is tiktoken's
    real tokenisation, which is tiktoken's behaviour, not ours.

    A test of the loader itself marks ``@pytest.mark.real_tiktoken_loader`` to
    opt out and exercise the real code (against files it builds in ``tmp_path``).

    Returns the encoding names requested, in order, so a test can assert the
    selection (e.g. gpt-4 -> cl100k_base) rather than only "n > 0".
    """
    requested: list[str] = []
    if request.node.get_closest_marker("real_tiktoken_loader"):
        return requested

    from primer.llm._tokenizer import _tiktoken_offline

    def _load_encoding(name: str, **_kwargs) -> tiktoken.Encoding:
        requested.append(name)
        return tiktoken.Encoding(
            name=f"offline-{name}",
            pat_str=r"(?s:.)",
            mergeable_ranks={bytes([i]): i for i in range(256)},
            special_tokens={},
        )

    monkeypatch.setattr(_tiktoken_offline, "load_encoding", _load_encoding)
    return requested
