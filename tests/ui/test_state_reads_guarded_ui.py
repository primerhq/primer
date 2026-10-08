"""A-22, console half: a non-admin's commit diff comes back as the header
with ``files: []`` and ``files_hidden: true``, and a raw read of a
``.state`` / ``.tmp`` path answers 403. Both diff views say the file
changes are admin-only (not "empty commit"), and the file doc shows the
refusal instead of an empty editor.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui" / "components"
FDOCS = (UI / "console" / "nv-file-docs.jsx").read_text(encoding="utf-8")
LEGACY = (UI / "workspaces.jsx").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    nxt = src.find("\nfunction ", start + 1)
    return src[start: nxt if nxt != -1 else len(src)]


def test_console_diff_doc_says_hidden_files_are_admin_only():
    body = _fn(FDOCS, "NV_DiffDoc")
    assert "files_hidden" in body
    assert "admins only" in body
    assert 'data-testid="nv-diff-hidden"' in body


def test_legacy_commit_diff_says_hidden_files_are_admin_only():
    body = _fn(LEGACY, "WS_CommitDiff")
    assert "files_hidden" in body
    assert "admins only" in body


def test_console_file_doc_renders_a_refused_read():
    body = _fn(FDOCS, "NV_FileDoc")
    assert "read.error" in body
    assert 'data-testid="nv-file-doc-error"' in body
