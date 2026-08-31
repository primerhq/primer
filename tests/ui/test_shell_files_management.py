"""Full file management on the workspace tree (revamp spec section 6).

All routes pre-existed (files/tree|read|download, PUT files with etag,
DELETE files, POST files/dir, POST files/move); this pins the frontend
seam (sh-api, the ONE url-naming module) and the console tree's
affordances (nv-files-sidebar since the flag day).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
API = (UI / "components" / "shell" / "sh-api.jsx").read_text(encoding="utf-8")
TREE = (UI / "components" / "console" / "nv-files-sidebar.jsx").read_text(
    encoding="utf-8")


def test_api_seam_covers_management():
    for fn in ("fileDelete", "makeDir", "fileMove", "fileUpload",
               "fileDownloadUrl"):
        assert fn + ":" in API, fn
    assert "files/move?src=" in API
    assert "files/dir?path=" in API
    assert '"DELETE"' in API


def test_write_supports_the_etag_precondition():
    m = re.search(r"fileWrite: function \(wid, path, content, etag\)", API)
    assert m, "fileWrite must accept the optional etag"
    assert "&etag=" in API


def test_tree_header_verbs():
    for tid in ("nv-file-new", "nv-file-upload", "nv-file-history"):
        assert f'data-testid="{tid}"' in TREE, tid


def test_row_menu_manages_the_entry():
    for label in ("Rename", "Delete", "Download", "Copy Path"):
        assert label in TREE, label
    assert "confirmDialog" in TREE, "delete confirms first"
    assert "promptDialog" in TREE


def test_drag_drop_upload():
    assert "onDrop" in TREE and "dataTransfer" in TREE
    assert "FileReader" in TREE, "dropped binaries go up as base64"
