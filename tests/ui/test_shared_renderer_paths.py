"""The surviving renderers live under shared/ (S1 P6 Task 32).

Transcript, Composer, SchemaPanel and the coalescing helpers were never
chat-specific; they were only ever stored there. Sessions are becoming
the sole conversational surface, so they move out before P7 deletes the
chat directory around them.

THE WINDOW GLOBALS ARE THE CONTRACT. Only the paths moved, which is why
this test pins paths: a later spec editing the old location would
otherwise fail silently at bundle time rather than here.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SHARED = UI / "components" / "shared"
MOVED = ("transcript.jsx", "composer.jsx", "schema-panel.jsx",
         "use-transcript.js")


def test_all_four_live_under_shared():
    for name in MOVED:
        assert (SHARED / name).exists(), f"ui/components/shared/{name} missing"


def test_nothing_references_the_old_chat_paths():
    """S4 and S8 both had tasks pinned at the pre-move paths; this is
    what makes those corrections detectable instead of silent."""
    stale = []
    for path in list(UI.rglob("*.jsx")) + list(UI.rglob("*.js")) + [
        UI / "index.html"
    ]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in MOVED:
            if f"components/chat/{name}" in text:
                stale.append(f"{path.relative_to(UI)} -> {name}")
    assert not stale, f"stale references to moved renderers: {stale}"


def test_index_loads_all_four_from_shared():
    text = (UI / "index.html").read_text(encoding="utf-8")
    for name in MOVED:
        assert f'src="components/shared/{name}"' in text, name


def test_helpers_still_load_before_the_renderer_that_uses_them():
    """Bundle order is load-bearing: the globals must exist first."""
    text = (UI / "index.html").read_text(encoding="utf-8")
    helpers = text.index('src="components/shared/use-transcript.js"')
    transcript = text.index('src="components/shared/transcript.jsx"')
    assert helpers < transcript


