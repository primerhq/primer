"""The shell's URL write effect reads an unread address-bar change before it writes (found in CI, 2026-10-08).

See ``tests/ui_e2e/test_shell_reads_an_early_hash_change_journey.py`` for the failure; this pins the mechanism.
"""

from __future__ import annotations

from pathlib import Path

SHELL = (Path(__file__).resolve().parents[2] / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")


def test_the_listener_effect_hands_its_reader_to_the_write_effect() -> None:
    assert "var readUrlRef = React.useRef(null);" in SHELL
    assert "readUrlRef.current = onNav;" in SHELL


def test_the_write_effect_reads_a_change_it_has_not_seen_instead_of_overwriting_it() -> None:
    write = SHELL[SHELL.index("// --- URL sync: write when our state moved"):]
    write = write[:write.index("// Menus close on any outside click.")]
    guard = "if (current !== ownHashRef.current) {"
    assert guard in write
    assert write.index(guard) < write.index("window.history.replaceState"), "the guard comes before any write"
    assert "readUrlRef.current()" in write[write.index(guard):write.index("window.history.replaceState")]
