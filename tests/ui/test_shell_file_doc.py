"""File doc editing discipline (revamp spec section 6).

Saves are etag-conditional (the read supplies the etag; a 412 raises
the changed-on-disk banner instead of clobbering); Ctrl+S saves;
binary or oversized files gate to a download instead of the editor.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (
    ROOT / "ui" / "components" / "shell" / "sh-doc-bodies.jsx"
).read_text(encoding="utf-8")


def test_save_is_etag_conditional():
    assert re.search(r"fileWrite\(shell\.wid, path, draft, force \? null : etag\)", SRC)


def test_412_raises_the_conflict_banner_not_a_clobber():
    assert "err.status === 412" in SRC
    assert 'data-testid="file-conflict-banner"' in SRC
    assert 'data-testid="file-conflict-reload"' in SRC
    assert 'data-testid="file-conflict-overwrite"' in SRC


def test_ctrl_s_saves():
    assert re.search(r'ev\.ctrlKey \|\| ev\.metaKey', SRC)
    assert '"s"' in SRC


def test_binary_and_large_files_gate_to_download():
    assert 'encoding === "base64"' in SRC
    assert "SH_FILE_EDIT_MAX_BYTES" in SRC
    assert 'data-testid="file-download-gate"' in SRC


def test_dirty_state_is_visible():
    assert 'data-testid="file-dirty"' in SRC
