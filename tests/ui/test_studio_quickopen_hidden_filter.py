"""QuickOpen (⌘P "Go to file…") must not list internal/hidden junk.

The finder fetches GET /workspaces/{wid}/files?recursive=true and used to list
git internals (.state/.git/*), .tmp, __pycache__, and dotfiles — which crowd out
real files and make the picker feel broken. The filter now skips any file whose
relative path contains a SEGMENT that begins with "." or equals "__pycache__".
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PALETTE = (UI / "components" / "studio-palette.jsx").read_text(encoding="utf-8")


def _quickopen_src() -> str:
    # Isolate QuickOpen's body so the guard is asserted on the ⌘P finder, not
    # the sibling StudioCommandPalette (⌘K) which lists sessions/actions.
    start = PALETTE.index("function QuickOpen(")
    return PALETTE[start:]


def test_quickopen_splits_path_into_segments() -> None:
    src = _quickopen_src()
    assert 'path.split("/")' in src


def test_quickopen_skips_dot_and_pycache_segments() -> None:
    # The exact exclusion predicate: any segment starting with "." (covers
    # .state, .git, .tmp, .tenacious, dotfiles) OR equal to "__pycache__".
    src = _quickopen_src()
    assert 'seg.charAt(0) === "."' in src
    assert 'seg === "__pycache__"' in src


def test_quickopen_still_fuzzy_matches_and_skips_dirs() -> None:
    # The junk-exclusion is additive: the directory skip + fuzzy match remain.
    src = _quickopen_src()
    assert "f.is_dir || f.isDir" in src
    assert "STP_fuzzy(path, query)" in src


def test_bundle_transpiles_with_quickopen_filter() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "window.QuickOpen = QuickOpen;" in text
    assert 'seg === "__pycache__"' in text
