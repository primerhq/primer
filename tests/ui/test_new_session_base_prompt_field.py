"""The two launch forms carry an optional base-prompt field that reaches the create body only when non-empty (Wiki First W4).

Source pin, in the house style of the other ``tests/ui/`` source tests: the
desktop overlay (``NV_CreateSessionOverlay``) and the mobile sheet
(``NV_MobileCreateSessionSheet``) each render a ``textarea`` for the
launch-time base prompt, and each submit body sends the ORIGINAL value -
trim decides omission only - so launch->row->prompt is byte-identical
(a blank field is omitted, mirroring the ``initial_instructions``
handling). A field that is present in the form but dropped from the body,
a body key set for a blank field, or a body that trims the value away,
fails here. The mobile textarea is a bare control with no wrapping label,
so it carries an explicit ``aria-label`` (a11y) that this pin holds.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = (ROOT / "ui" / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
MOBILE = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")

# The submit-body line is identical in both forms: trim decides omission
# only, and the ORIGINAL value is what the create body carries.
BODY_LINE = "if (basePrompt.trim()) body.base_prompt = basePrompt;"


def test_the_desktop_overlay_renders_the_field_and_sends_the_original_bytes_only_when_non_empty() -> None:
    assert 'data-testid="nv-ns-base-prompt"' in OVERLAYS
    assert BODY_LINE in OVERLAYS


def test_the_mobile_sheet_renders_the_field_and_sends_the_original_bytes_only_when_non_empty() -> None:
    assert 'data-testid="nv-mob-ns-base-prompt"' in MOBILE
    assert BODY_LINE in MOBILE


def test_the_mobile_field_carries_an_accessible_label() -> None:
    # The bare textarea has no wrapping <label>; the aria-label is its accessible name.
    assert 'aria-label="Base prompt"' in MOBILE


def test_the_mobile_sheet_reset_clears_the_field() -> None:
    assert 'setBasePrompt("")' in MOBILE
