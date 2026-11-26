"""The shim: one JSON round trip, limits set before any user code runs.

These run the real interpreter the runner will use, because the properties
under test (primer's packages being unreachable, limits being irreversible)
are properties of the process, not of any Python object.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from primer.toolset.python_runner._shim import SHIM_SOURCE

MODULE = '''
def greet(name: str) -> str:
    return "hello " + name

def boom(x: str) -> str:
    raise ValueError("nope")

def unserialisable(x: str):
    return object()

def ask(question: str, ctx=None):
    return ask_user(question)
'''


def _run(request: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", SHIM_SOURCE],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _req(fn: str, args: dict, **extra) -> dict:
    base = {
        "module": MODULE,
        "fn": fn,
        "phase": "call",
        "args": args,
        "ctx": {"tool_call_id": "tc-1", "session_id": "s-1"},
        "limits": {"cpu_seconds": 5, "address_space_bytes": 512 * 1024 * 1024},
    }
    base.update(extra)
    return base


def test_a_plain_call_returns_its_value() -> None:
    assert _run(_req("greet", {"name": "ada"})) == {"ok": True, "value": "hello ada"}


def test_an_exception_becomes_a_structured_error() -> None:
    out = _run(_req("boom", {"x": "1"}))
    assert out["ok"] is False
    assert out["error"]["type"] == "ValueError"
    assert "nope" in out["error"]["message"]
    assert out["error"]["traceback"]


def test_a_non_serialisable_return_is_an_error_not_a_crash() -> None:
    out = _run(_req("unserialisable", {"x": "1"}))
    assert out["ok"] is False
    assert "serialis" in out["error"]["message"].lower()


def test_ask_user_returns_a_yield_request_not_a_value() -> None:
    out = _run(_req("ask", {"question": "ship it?"}))
    assert out["ok"] is True
    assert out["yield"]["kind"] == "ask_user"
    assert out["yield"]["params"]["question"] == "ship it?"


def test_ctx_is_injected_only_when_declared() -> None:
    mod = (
        "def with_ctx(a: str, ctx=None):\n"
        "    return ctx['session_id']\n"
        "def without_ctx(a: str):\n"
        "    return 'no ctx'\n"
    )
    assert _run(_req("with_ctx", {"a": "1"}) | {"module": mod})["value"] == "s-1"
    assert _run(_req("without_ctx", {"a": "1"}) | {"module": mod})["value"] == "no ctx"


def test_the_shim_cannot_import_primer() -> None:
    # -I -S with no PYTHONPATH: primer's site-packages must be off sys.path.
    # Without this a tool could `import primer.storage` and reach the database
    # with ambient credentials, which no rlimit or syscall filter would stop.
    mod = "def probe(x: str) -> str:\n    import primer\n    return 'reached'\n"
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["ok"] is False
    assert out["error"]["type"] in {"ModuleNotFoundError", "ImportError"}


def test_limits_are_set_with_soft_equal_to_hard() -> None:
    # A soft-only limit is decorative: user code could raise it back to the
    # hard value with one setrlimit call.
    mod = (
        "import resource\n"
        "def probe(x: str) -> list:\n"
        "    return list(resource.getrlimit(resource.RLIMIT_CPU))\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["value"][0] == out["value"][1] == 5


def test_a_tool_cannot_raise_its_own_limits() -> None:
    mod = (
        "import resource\n"
        "def probe(x: str) -> str:\n"
        "    try:\n"
        "        resource.setrlimit(resource.RLIMIT_CPU, (600, 600))\n"
        "        return 'raised'\n"
        "    except (ValueError, OSError):\n"
        "        return 'refused'\n"
    )
    assert _run(_req("probe", {"x": "1"}) | {"module": mod})["value"] == "refused"


def test_an_unknown_function_is_an_error() -> None:
    out = _run(_req("nope", {}))
    assert out["ok"] is False
    assert "nope" in out["error"]["message"]


def test_a_module_that_fails_to_load_is_an_error_not_a_crash() -> None:
    mod = "raise RuntimeError('bad module')\n"
    out = _run(_req("anything", {}) | {"module": mod})
    assert out["ok"] is False
    assert "failed to load" in out["error"]["message"]


def test_the_resume_phase_passes_payload_and_meta() -> None:
    mod = (
        "def r(payload: dict, meta: dict) -> str:\n"
        "    return payload['response'] + '/' + str(meta['n'])\n"
    )
    out = _run(
        _req("r", {})
        | {"module": mod, "phase": "resume", "payload": {"response": "yes"},
           "meta": {"n": 7}}
    )
    assert out["value"] == "yes/7"


def test_an_async_tool_is_awaited() -> None:
    mod = "async def slow(a: str) -> str:\n    return 'async ' + a\n"
    assert _run(_req("slow", {"a": "x"}) | {"module": mod})["value"] == "async x"


# ---------------------------------------------------------------------------
# seccomp: the syscall filter, where the host has libseccomp
# ---------------------------------------------------------------------------


def _seccomp_available() -> bool:
    import ctypes.util

    return bool(ctypes.util.find_library("seccomp"))


SECCOMP_ONLY = pytest.mark.skipif(
    not _seccomp_available(), reason="libseccomp not present on this host"
)


@SECCOMP_ONLY
def test_outbound_connections_are_blocked_by_default() -> None:
    # socket() with a network family is refused at creation, so the tool
    # cannot even hold an fd to connect with.
    mod = (
        "import socket\n"
        "def probe(x: str) -> str:\n"
        "    try:\n"
        "        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    except PermissionError:\n"
        "        return 'blocked'\n"
        "    s.close()\n"
        "    try:\n"
        "        s.settimeout(2)\n"
        "        s.connect(('192.0.2.1', 80))\n"
        "        return 'connected'\n"
        "    except PermissionError:\n"
        "        return 'blocked'\n"
        "    except OSError as exc:\n"
        "        return 'other:' + type(exc).__name__\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["value"] == "blocked"


@SECCOMP_ONLY
def test_creating_a_unix_socket_is_allowed_so_async_tools_work() -> None:
    # AF_UNIX stays allowed: asyncio builds its event loop self-pipe from a
    # socketpair, and local pairs carry no network bytes.
    mod = (
        "import socket\n"
        "def probe(x: str) -> str:\n"
        "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "    s.close()\n"
        "    return 'created'\n"
    )
    assert _run(_req("probe", {"x": "1"}) | {"module": mod})["value"] == "created"


@SECCOMP_ONLY
def test_named_bypass_syscalls_are_refused() -> None:
    # The 2026-11-23 named bypasses (5.10): sendmmsg/recvmmsg move bytes
    # without the denied sendto/sendmsg family, and io_uring does too. All
    # must answer EPERM (errno 1) from the filter, not run.
    mod = (
        "import ctypes, socket\n"
        "def probe(x: str) -> str:\n"
        "    out = []\n"
        "    libc = ctypes.CDLL(None, use_errno=True)\n"
        "    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)\n"
        "    fd = s.fileno()\n"
        "    null = ctypes.c_void_p(0)\n"
        "    # The filter judges the syscall before the kernel touches its\n"
        "    # arguments, so the (uninitialised) pointer values are irrelevant.\n"
        "    # A ctypes call never raises: a refused syscall returns -1 with errno set.\n"
        "    for name in ('sendmmsg', 'recvmmsg'):\n"
        "        rc = getattr(libc, name)(fd, null, 0, 0)\n"
        "        out.append(name + ':' + (str(ctypes.get_errno()) if rc == -1 else 'allowed'))\n"
        "    return '/'.join(out)\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["value"] == "sendmmsg:1/recvmmsg:1", out


@SECCOMP_ONLY
def test_a_network_socket_cannot_be_created_or_written() -> None:
    # The deeper half of 5.10: read/write on a socket fd works on whatever fd
    # the tool holds, so a network fd must not exist at all. AF_INET creation
    # is refused, and the tool cannot fall back to writing a bound socket.
    mod = (
        "import socket\n"
        "def probe(x: str) -> str:\n"
        "    try:\n"
        "        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "        return 'created'\n"
        "    except PermissionError:\n"
        "        return 'blocked'\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["value"] == "blocked"


@SECCOMP_ONLY
def test_an_async_tool_still_runs_under_the_filter() -> None:
    # Regression: denying socket() blocked asyncio's self-pipe and broke every
    # async tool, which is the shape a yielding tool is written in.
    mod = "async def slow(a: str) -> str:\n    return 'async ' + a\n"
    out = _run(_req("slow", {"a": "x"}) | {"module": mod})
    assert out["ok"] is True, out.get("error")
    assert out["value"] == "async x"


@SECCOMP_ONLY
def test_allow_network_permits_connect() -> None:
    mod = (
        "import socket\n"
        "def probe(x: str) -> str:\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    try:\n"
        "        s.settimeout(2)\n"
        "        s.connect(('192.0.2.1', 80))\n"
        "        return 'connected'\n"
        "    except PermissionError:\n"
        "        return 'blocked'\n"
        "    except OSError:\n"
        "        return 'reached-network'\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod, "allow_network": True})
    # 192.0.2.0/24 is TEST-NET-1 and never routes, so the call times out or is
    # refused. Either proves the syscall was permitted; only EPERM would not.
    assert out["value"] == "reached-network"


@SECCOMP_ONLY
def test_a_process_clone_is_refused_and_a_thread_still_starts() -> None:
    # The clone rule refuses a clone without CLONE_THREAD (a new process) and lets a thread through. execve is
    # refused separately, so a subprocess test passes even with a clone rule that refuses nothing: os.fork() is
    # the probe that only the clone rule can stop.
    mod = (
        "import os, threading\n"
        "def probe(x: str) -> str:\n"
        "    t = threading.Thread(target=lambda: None)\n"
        "    t.start()\n"
        "    t.join()\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        return 'thread-ok-fork-refused'\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    os.waitpid(pid, 0)\n"
        "    return 'forked'\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["value"] == "thread-ok-fork-refused", out


@SECCOMP_ONLY
def test_spawning_a_subprocess_is_blocked() -> None:
    mod = (
        "import subprocess\n"
        "def probe(x: str) -> str:\n"
        "    try:\n"
        "        subprocess.run(['/bin/true'])\n"
        "        return 'spawned'\n"
        "    except Exception:\n"
        "        return 'blocked'\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["value"] == "blocked"


@SECCOMP_ONLY
def test_ordinary_work_still_runs_under_the_filter() -> None:
    # A default-deny filter would have to enumerate everything CPython needs;
    # this asserts the deny-list approach has not broken normal execution.
    mod = (
        "import json, math, re\n"
        "def probe(x: str) -> str:\n"
        "    data = json.dumps({'v': math.sqrt(16)})\n"
        "    return re.sub(r'\\\\s+', '', data)\n"
    )
    out = _run(_req("probe", {"x": "1"}) | {"module": mod})
    assert out["ok"] is True
    assert "4.0" in out["value"]
