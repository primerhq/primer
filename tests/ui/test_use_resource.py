"""ui/foundation/use-resource.js — resourceState().

2026-09-10 audit: 0 of 192 useResource call sites app-wide read `degraded`
at all (see tests/ui/test_console_system.py's own worker-pool history for
the first instance of this class). Every one of them therefore shows its
loading affordance forever on sustained fetch failure, indistinguishable
from a fetch that's merely in flight. resourceState() collapses a
useResource snapshot into the three states a consumer actually needs -
ready / stuck / loading - so a new consumer doesn't have to reinvent (or
omit) the distinction.

Pure logic, no React/timers involved in the function itself, so it is
EXECUTED here via MiniRacer rather than substring-matched, mirroring
tests/ui/test_shell_url.py's own convention for foundation/*.js. The
module references window.React's hooks at load time (never calls them
outside an actual component render), so a minimal stub is enough to load
it without executing any component.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "use-resource.js"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(
        "window.React = { useState: function(){}, useEffect: function(){}, "
        "useRef: function(){}, useCallback: function(f){ return f; } };"
    )
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _state(snap: dict) -> str:
    ctx = _ctx()
    ctx.eval(f"var __snap = {json.dumps(snap)};")
    return ctx.eval("window.primerApi.resourceState(__snap)")


def test_registered_on_primer_api() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert "ns.resourceState = resourceState" in src
    assert 'src="foundation/use-resource.js"' in (
        (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    )


def test_data_present_is_ready_even_while_erroring() -> None:
    # Stale-while-error: a later poll failing must not un-render data
    # that already arrived. This is the case that matters for the four
    # nv-system.jsx cards already traced clean (2026-09-10) - their own
    # data sources cannot resolve to a non-null value that isn't real, so
    # "ready" is correct for them whenever data is present at all.
    assert _state({"data": {"total": 3}, "error": None, "degraded": False}) == "ready"
    assert _state({"data": {"total": 3}, "error": {"title": "boom"}, "degraded": True}) == "ready"
    assert _state({"data": 0, "error": None, "degraded": False}) == "ready", (
        "falsy-but-present data (0, empty list/dict) must still be ready, not loading"
    )


def test_no_data_and_degraded_is_stuck() -> None:
    # The permanent-lie case: MAX_ERRORS consecutive failures, backing
    # off, never resolving within human-observable time.
    assert _state({"data": None, "error": {"title": "boom"}, "degraded": True}) == "stuck"


def test_no_data_and_not_degraded_is_loading() -> None:
    # First fetch in flight - the honest case.
    assert _state({"data": None, "error": None, "degraded": False}) == "loading"
    # A single failure (or two) that hasn't crossed MAX_ERRORS yet: still
    # transient by the hook's own design (exponential backoff, not yet
    # the debounced "genuinely stuck" signal).
    assert _state({"data": None, "error": {"title": "boom"}, "degraded": False}) == "loading"


def test_missing_fields_default_to_loading_not_a_crash() -> None:
    # A hand-built snapshot (e.g. a test double) that omits `degraded`
    # entirely must not throw - it reads as loading, the safe default.
    assert _state({"data": None}) == "loading"
