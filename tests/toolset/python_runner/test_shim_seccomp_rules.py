"""The shim's seccomp RULES, driven through a fake libseccomp.

The real-enforcement tests in test_shim.py need libseccomp (CI/Linux). These
run the same SHIM_SOURCE with a fake library that records every rule it is
asked to add, so the rule set itself - the named 2026-11-23 bypasses (5.10)
and the fail-closed setup failures - is asserted on any host.

The subprocess runs the real interpreter with a bootstrap file that patches
``ctypes.CDLL`` and ``ctypes.util.find_library`` before the shim body runs;
the shim never knows it is holding a fake.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from primer.toolset.python_runner._shim import SHIM_SOURCE

# Deterministic name -> syscall-number mapping, known to the test too:
# never negative (the shim treats < 0 as "not on this arch"), never zero.
def _nr(name: str) -> int:
    return zlib.crc32(name.encode()) % 1000003 + 1


_BOOTSTRAP = r'''
import ctypes, ctypes.util, json, os, zlib


class _Fn:
    # A callable that still accepts ctypes' restype/argtypes assignment.
    def __init__(self, fn):
        self._fn = fn

    def __call__(self, *args):
        return self._fn(*args)


def _i(v):
    # The shim passes ctypes instances (int() on one is not portable); a
    # plain int is tolerated for safety.
    return v.value if hasattr(v, "value") and not isinstance(v, (int, bytes)) else int(v)


class _FakeLib:
    def __init__(self):
        self.calls = []
        self.fail = os.environ.get("FAKE_SECCOMP_FAIL", "")

    def _init(self, act):
        self.calls.append(["init", _i(act)])
        return 0 if self.fail == "init" else 0x1234

    def _resolve(self, name):
        return zlib.crc32(bytes(name)) % 1000003 + 1

    def _rule_add(self, ctx, act, nr, idx):
        self.calls.append(["rule_add", _i(nr), _i(idx)])
        return 1 if self.fail == "rule" else 0

    def _rule_add_array(self, ctx, act, nr, idx, cmp):
        c = cmp._obj        # byref -> the shim's _ArgCmp struct
        self.calls.append(["rule_add_array", _i(nr), _i(idx),
                           int(c.arg), int(c.op), int(c.datum_a), int(c.datum_b)])
        return 1 if self.fail == "rule" else 0

    def _load(self, ctx):
        self.calls.append(["load"])
        return 1 if self.fail == "load" else 0

    def _release(self, ctx):
        self.calls.append(["release"])
        path = os.environ.get("FAKE_SECCOMP_CALLS")
        if path:
            with open(path, "w") as fh:
                json.dump(self.calls, fh)


_LIB = _FakeLib()
_LIB.seccomp_init = _Fn(_LIB._init)
_LIB.seccomp_syscall_resolve_name = _Fn(_LIB._resolve)
_LIB.seccomp_rule_add = _Fn(_LIB._rule_add)
_LIB.seccomp_rule_add_array = _Fn(_LIB._rule_add_array)
_LIB.seccomp_load = _Fn(_LIB._load)
_LIB.seccomp_release = _Fn(_LIB._release)

_REAL_CDLL = ctypes.CDLL


def _cdll(name, *args, **kwargs):
    fake = os.environ.get("FAKE_SECCOMP_NAME")
    if fake and name == fake:
        return _LIB
    return _REAL_CDLL(name, *args, **kwargs)


ctypes.CDLL = _cdll
_real_find = ctypes.util.find_library
_fake_name = os.environ.get("FAKE_SECCOMP_NAME")
if _fake_name:
    ctypes.util.find_library = (
        lambda libname: _fake_name if libname == "seccomp" else _real_find(libname)
    )
else:
    ctypes.util.find_library = (
        lambda libname: None if libname == "seccomp" else _real_find(libname)
    )
'''

MODULE = '''
def greet(name: str) -> str:
    return "hello " + name
'''

NAMED_BYPASSES = ("sendmmsg", "recvmmsg", "io_uring_setup", "io_uring_enter", "io_uring_register")

AF_UNIX, AF_INET, AF_INET6 = 1, 2, 10
AF_NETLINK, AF_PACKET, AF_VSOCK = 16, 17, 40

SCMP_CMP_EQ, SCMP_CMP_NE, SCMP_CMP_MASKED_EQ = 0, 1, 7


def _socket_family_refused(calls: list, family: int) -> bool:
    """Whether a recorded rule set refuses socket(family).

    The filter is default-allow, so a family is refused when a rule on the
    socket() syscall comparing argument 0 (the family) matches it.
    """
    nr = _nr("socket")
    for e in calls:
        if e[0] != "rule_add_array" or e[1] != nr or e[3] != 0:
            continue
        op, datum_a, datum_b = e[4], e[5], e[6]
        if (op == SCMP_CMP_EQ and family == datum_a
                or op == SCMP_CMP_NE and family != datum_a
                or op == SCMP_CMP_MASKED_EQ and (family & datum_a) == datum_b):
            return True
    return False


def _req(**extra) -> dict:
    return {
        "module": MODULE,
        "fn": "greet",
        "phase": "call",
        "args": {"name": "ada"},
        "ctx": {"tool_call_id": "tc-1", "session_id": "s-1"},
        "limits": {"cpu_seconds": 5, "address_space_bytes": 512 * 1024 * 1024},
        **extra,
    }


def _run_fake(request: dict, tmp_path: Path, *, fail: str = "", available: bool = True) -> tuple[dict, list | None]:
    bootstrap = tmp_path / "bootstrap.py"
    calls_path = tmp_path / "calls.json"
    bootstrap.write_text(_BOOTSTRAP + SHIM_SOURCE)
    env = {"PATH": "/usr/bin:/bin"}
    if available:
        env["FAKE_SECCOMP_NAME"] = "libseccomp-fake"
        env["FAKE_SECCOMP_CALLS"] = str(calls_path)
    if fail:
        env["FAKE_SECCOMP_FAIL"] = fail
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(bootstrap)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    calls = json.loads(calls_path.read_text()) if calls_path.exists() else None
    return json.loads(proc.stdout), calls


def test_named_bypass_syscalls_get_denial_rules(tmp_path) -> None:
    out, calls = _run_fake(_req(), tmp_path)
    assert out == {"ok": True, "value": "hello ada"}
    assert calls is not None and "load" in [c[0] for c in calls]
    plain = {e[1] for e in calls if e[0] == "rule_add"}
    # The named bypasses are denied by number, and the ordinary list survives.
    for name in NAMED_BYPASSES:
        assert _nr(name) in plain, name
    assert _nr("execve") in plain
    # The network fd is closed at creation: one rule refuses socket() for
    # every family except AF_UNIX (round 1 of 2026-11-23; the earlier
    # allow-list of AF_INET/AF_INET6 left the other families open).
    socket_rules = {(e[4], e[5]) for e in calls
                    if e[0] == "rule_add_array" and e[1] == _nr("socket") and e[3] == 0}
    assert socket_rules == {(SCMP_CMP_NE, AF_UNIX)}


def test_socket_is_denied_for_every_family_except_af_unix(tmp_path) -> None:
    # Round 1 (review I1): AF_NETLINK, AF_PACKET and AF_VSOCK stayed
    # creatable with network denied under the AF_INET/AF_INET6 allow-list.
    out, calls = _run_fake(_req(), tmp_path)
    assert out == {"ok": True, "value": "hello ada"}
    for family in (AF_INET, AF_INET6, AF_NETLINK, AF_PACKET, AF_VSOCK):
        assert _socket_family_refused(calls, family), family
    # The benign case: AF_UNIX stays allowed (asyncio builds its event-loop
    # self-pipe from an AF_UNIX pair; socketpair() is a different syscall
    # and is not denied at all).
    assert not _socket_family_refused(calls, AF_UNIX)


def test_allow_network_omits_the_network_rules(tmp_path) -> None:
    out, calls = _run_fake(_req(allow_network=True), tmp_path)
    assert out == {"ok": True, "value": "hello ada"}
    plain = {e[1] for e in calls if e[0] == "rule_add"}
    for name in NAMED_BYPASSES:
        assert _nr(name) not in plain, name
    # The filter is still built (the process denies are there)...
    assert _nr("execve") in plain
    # ...but no rule on socket's family argument at all.
    assert not [e for e in calls
                if e[0] == "rule_add_array" and e[1] == _nr("socket") and e[3] == 0]


@pytest.mark.parametrize("fail", ["init", "rule", "load"])
def test_a_failed_filter_setup_refuses_the_call(tmp_path, fail: str) -> None:
    # A host WITH libseccomp where the filter cannot be built or loaded must
    # refuse the call, never run the tool unconfined (review 2026-11-23, 5.10).
    out, _ = _run_fake(_req(), tmp_path, fail=fail)
    assert out["ok"] is False
    assert out["error"]["type"] == "RunnerError"
    assert "seccomp" in out["error"]["message"]


def test_unavailable_seccomp_keeps_running(tmp_path) -> None:
    # No libseccomp at all: today's documented behaviour (the tool runs, the
    # note is logged host-side) and no library is touched.
    out, calls = _run_fake(_req(), tmp_path, available=False)
    assert out == {"ok": True, "value": "hello ada"}
    assert calls is None
