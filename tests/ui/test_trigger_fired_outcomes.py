"""What an answered fire_now says on the detail page and the phone fact sheet (ticket 01a12144).

Two surfaces said 'Fired' when the server SKIPPED the fire: a disabled trigger's fire_now answers
200 {skipped: true, fire_id: null, results: []}, and the triggers DETAIL page printed the bare
'Fired' while the phone fact sheet toasts 'Fired <id>'. The list row already gets this right: it
reports the fire through TR_fireOutcome (a skipped fire is the warning 'Not fired' with the why,
a fire whose deliveries failed is 'Fired, with failures', otherwise 'Trigger fired'). The two
surfaces now use the same helper.

The V8 cases run the real triggers.jsx TR_fireOutcome in MiniRacer with the real fire_now bodies
from tests/_support/trigger_envelopes.py (the way #695's tests do); the surfaces are pinned
statically, the way this directory pins the rest of the console.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")
MOB = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


@pytest.fixture(scope="module")
def bodies() -> dict[str, dict]:
    from tests._support.trigger_envelopes import real_fire_bodies

    return real_fire_bodies()


def _outcome(res: dict, label: str) -> dict:
    """The real TR_fireOutcome from triggers.jsx, run in V8 (MiniRacer) on the given body."""
    from py_mini_racer import MiniRacer

    start = SRC.index("function TR_fireOutcome(")
    end = SRC.index("\n}\n", start) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = {};")
    ctx.eval(SRC[start:end])
    return json.loads(ctx.eval(f"JSON.stringify(TR_fireOutcome({json.dumps(res)}, {json.dumps(label)}))"))


# ---- TR_fireOutcome says what the server answered (V8, the real bodies) -----------------------------------------


def test_a_disabled_trigger_says_not_fired(bodies) -> None:
    """The 200 {skipped: true, fire_id: null} body: nothing was fired, and the why is said, not 'Fired'."""
    out = _outcome(bodies["skipped"], "nightly")
    assert out["kind"] == "warning"
    assert out["title"] == "Not fired"
    assert out["detail"] == "nightly is disabled; enable it to fire it."


def test_a_fired_trigger_says_fired(bodies) -> None:
    assert _outcome(bodies["fired"], "nightly") == {
        "kind": "success",
        "title": "Trigger fired",
        "detail": "nightly",
    }


# ---- the detail page reuses the outcome for a skipped fire -------------------------------------------------------


def test_the_detail_page_branches_on_the_skipped_outcome() -> None:
    """A skipped fire renders the TR_fireOutcome words ('Not fired', and why), not the bare 'Fired'."""
    block = SRC[SRC.index('data-testid="fire-now-result"'):SRC.index('data-testid="fire-now-results-list"')]
    assert "fireResult.skipped" in block
    assert "TR_fireOutcome" in block


# ---- the phone fact sheet reads the outcome and the one refusal reader ---------------------------------------------


def test_the_mobile_fact_sheet_uses_the_outcome_and_the_one_reader() -> None:
    """A skipped fire is not 'Fired <id>'; a refused one is the one reader's text, with its request id."""
    start = MOB.index("function fireNow()")
    end = MOB.index("\n  }\n", start) + 4
    fn = MOB[start:end]
    assert '"Fired " + (props.row.id' not in fn
    assert '"Fire failed: " + ((e && e.message)' not in fn
    assert "TR_fireOutcome" in fn
    assert "TR_refusalText" in fn
    assert "requestId" in fn


def test_the_mobile_shell_can_reach_the_trigger_helpers() -> None:
    """TR_fireOutcome / TR_refusalText are module-local to triggers.jsx; the console page needs the window exports."""
    assert "window.TR_fireOutcome = TR_fireOutcome" in SRC
    assert "window.TR_refusalText = TR_refusalText" in SRC
