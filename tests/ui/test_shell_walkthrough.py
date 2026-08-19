"""First-run and empty states (spec section 8).

The walkthrough IS the operator session: a seeded operator turn with a
3-5 step checklist whose steps are live verb invocations. No separate
welcome page, no onboarding sprawl, and every empty state is a prompt
with an action.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
MODULE = UI / "foundation" / "shell-walkthrough.js"
SRC = UI / "components" / "shell" / "sh-walkthrough.jsx"
SHELL = UI / "components" / "shell"


def _module() -> str:
    return MODULE.read_text(encoding="utf-8")


def test_it_is_three_to_five_steps() -> None:
    """Short, verb-labeled, one walkthrough. Onboarding sprawl is an
    explicit antipattern."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(_module())
    steps = json.loads(ctx.eval("JSON.stringify(SH_WALKTHROUGH_STEPS)"))
    assert 3 <= len(steps) <= 5
    for step in steps:
        assert set(step) == {"id", "label", "verbId"}


def test_every_step_names_a_verb_that_some_task_registers() -> None:
    """A step pointing at a dead verb is a broken first run."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(_module())
    wanted = set(json.loads(ctx.eval(
        'JSON.stringify(SH_WALKTHROUGH_STEPS.map(function (s) '
        '{ return s.verbId; }))'
    )))
    registered: set[str] = set()
    for path in sorted(SHELL.glob("*.jsx")):
        body = path.read_text(encoding="utf-8")
        registered |= set(re.findall(r'id:\s*"([\w.]+)",\s*label:', body))
    missing = wanted - registered
    assert not missing, f"walkthrough points at unregistered verbs: {missing}"


def test_there_is_no_separate_welcome_page() -> None:
    for path in sorted(SHELL.glob("*.jsx")):
        body = path.read_text(encoding="utf-8").lower()
        for banned in ("welcomepage", "onboardingpage", "gettingstartedpage"):
            assert banned not in body, f"{path.name}: {banned}"


def test_completion_is_derived_from_the_transcript_not_a_flag() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(_module())
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_walkthroughState([{kind: "user_message", '
        'seq: 1, payload: {walkthrough_seed: true}}]))'
    ))
    assert out["active"] is True
    quiet = json.loads(ctx.eval(
        'JSON.stringify(SH_walkthroughState([{kind: "user_message", '
        'seq: 1, payload: {}}]))'
    ))
    assert quiet["active"] is False


def test_the_checklist_runs_the_verb_rather_than_describing_it() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert "registry.get(" in src
    assert ".run()" in src
    assert 'data-testid={"shell-walkthrough-step:"' in src


def test_every_empty_state_offers_an_action() -> None:
    """Section 8: every empty state is a prompt with an action."""
    for path in sorted(SHELL.glob("*.jsx")):
        body = path.read_text(encoding="utf-8")
        for match in re.finditer(r'className="sh-empty"[^>]*>([^<]*)', body):
            text = match.group(1).strip()
            if not text:
                continue
            assert re.search(r"Ctrl\+|Press |Open |Create |Pick ", text), (
                f"{path.name}: dead-end empty state {text!r}"
            )
