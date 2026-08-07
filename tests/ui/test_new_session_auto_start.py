"""Regression: the new-session 'Start immediately' control.

Creating a session and running it are separate acts, and a session with no
instructions has nothing to run on -- so the control is off by default. It
follows the instructions field until the operator states a preference,
after which their choice sticks.

Static-source checks only, matching the rest of the ui/ suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ui" / "components" / "new-session-form.jsx"
CSS = ROOT / "ui" / "styles.css"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_auto_start_defaults_off() -> None:
    assert "React.useState(false)" in _src().split("autoStart")[1][:60], (
        "autoStart must default to false"
    )


def test_auto_start_follows_instructions_until_touched() -> None:
    src = _src()
    assert "autoStartTouched" in src, "needs a touched flag to stop fighting the user"
    assert "if (autoStartTouched.current) return;" in src
    assert "setAutoStart(instructions.trim().length > 0);" in src
    assert "[instructions]" in src, "the effect must depend on instructions"


def test_toggle_marks_touched_so_the_choice_sticks() -> None:
    src = _src()
    # Both the switch and its label set the flag before flipping state.
    assert src.count("autoStartTouched.current = true;") >= 2


def test_rendered_as_a_switch_not_a_checkbox() -> None:
    src = _src()
    assert 'role="switch"' in src
    assert "aria-checked={autoStart}" in src
    assert 'data-testid="session-auto-start"' in src
    assert 'type="checkbox"\n            checked={autoStart}' not in src


def test_switch_styling_exists_and_is_reduced_motion_safe() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert ".switch-toggle {" in css
    assert ".switch-toggle.on" in css
    assert ".switch-toggle:focus-visible" in css, "keyboard focus must be visible"
    assert "min-height: 44px;" in css.split(".switch-row {")[1][:200], (
        "the row must clear the 44px touch-target floor"
    )
    reduced = css.split("@media (prefers-reduced-motion: reduce)")
    assert any(".switch-toggle" in blk for blk in reduced[1:])
