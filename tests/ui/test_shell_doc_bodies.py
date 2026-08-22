"""Three of the four doc kinds.

Section 3 names them viewer/editor, diff view and collection document;
section 8 makes their anchors addressable (#L10-L30) and their comparison
a TAB, never an overlay.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-doc-bodies.jsx"
API = UI / "components" / "shell" / "sh-api.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_and_every_doc_kind_now_has_a_body() -> None:
    src = _src()
    for name in ("SH_FileDoc", "SH_DiffDoc", "SH_WikiDoc"):
        assert "window." + name + " = " + name + ";" in src, name
    assert 'src="components/shell/sh-doc-bodies.jsx"' in (
        UI / "index.html"
    ).read_text(encoding="utf-8")


def test_the_file_doc_honours_the_line_anchor() -> None:
    src = _src()
    assert "SH_parseAnchor" in src
    assert 'data-testid={"shell-file-line:"' in src


def test_editing_a_file_promotes_its_preview_tab() -> None:
    """VS Code semantics: edit promotes. Otherwise a typed character is
    lost the moment the next single click reuses the preview slot."""
    src = _src()
    assert "promoteDoc" in src
    assert "SH_api.fileWrite" in src


def test_the_save_path_matches_the_shipped_handler() -> None:
    api = API.read_text(encoding="utf-8")
    assert re.search(r"fileWrite:\s*function\s*\(wid,\s*path,\s*content\)", api)
    assert '"PUT"' in api
    assert 'encoding: "text"' in api


def test_the_wiki_doc_reads_a_collection_document_by_path() -> None:
    api = API.read_text(encoding="utf-8")
    assert re.search(
        r"collectionDocument:\s*function\s*\(cid,\s*path,\s*signal\)", api
    )
    assert '"/documents?path="' in api
    # wiki:<cid>/<slug-path>: the first segment names the collection.
    assert 'indexOf("/")' in _src()


def test_the_diff_doc_renders_per_file_patches_side_by_side() -> None:
    src = _src()
    assert "SH_api.commit" in src
    assert 'data-testid={"shell-diff:"' in src
    assert "sh-diff-split" in src
