"""Full file management on the workspace tree (revamp spec section 6).

All routes pre-existed (files/tree|read|download, PUT files with etag,
DELETE files, POST files/dir, POST files/move); this pins the frontend
seam and the tree affordances.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
API = (UI / "components" / "shell" / "sh-api.jsx").read_text(encoding="utf-8")
RAIL = (UI / "components" / "shell" / "sh-rail.jsx").read_text(encoding="utf-8")


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
    for tid in ("files-new-file", "files-new-folder", "files-upload",
                "files-refresh"):
        assert f'data-testid="{tid}"' in RAIL, tid


def test_row_menu_manages_the_entry():
    assert "SH_FileRowMenu" in RAIL
    for label in ("Rename", "Delete", "Download", "Copy Path"):
        assert label in RAIL, label
    assert "confirmDialog" in RAIL, "delete confirms first"
    assert "promptDialog" in RAIL


def test_drag_drop_upload():
    assert "onDrop" in RAIL and "dataTransfer" in RAIL
    assert "FileReader" in RAIL, "dropped binaries go up as base64"
