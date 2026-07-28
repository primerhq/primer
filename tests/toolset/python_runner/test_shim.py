"""The shim: one JSON round trip, limits set before any user code runs.

These run the real interpreter the runner will use, because the properties
under test (primer's packages being unreachable, limits being irreversible)
are properties of the process, not of any Python object.
"""

from __future__ import annotations

import json
import subprocess
import sys

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
