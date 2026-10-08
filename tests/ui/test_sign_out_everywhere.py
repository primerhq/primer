"""The profile menu offers "Sign out everywhere" (SEC-05).

Static pins: the menu row exists next to "Log out", confirms first (it
ends the user's other sessions too), posts to the revoke-all endpoint, and
reloads to the sign-in screen whatever the answer.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHROME = (ROOT / "ui" / "components" / "console" / "nv-chrome.jsx").read_text(encoding="utf-8")


def _row() -> str:
    m = re.search(r'data-testid="nv-logout-all"[\s\S]{0,900}', CHROME)
    assert m, "the profile menu has no sign-out-everywhere row"
    return m.group(0)


def test_the_row_sits_after_log_out():
    assert CHROME.index('data-testid="nv-logout"') < CHROME.index('data-testid="nv-logout-all"')


def test_the_row_confirms_then_posts_to_logout_all_and_reloads():
    row = _row()
    assert "Sign out everywhere" in row
    assert "window.confirmDialog(" in row  # the themed dialog, never the browser's confirm()
    assert "window.confirm(" not in row
    assert '"/v1/auth/logout-all"' in row
    assert 'method: "POST"' in row
    assert row.count("window.location.reload()") >= 2  # success and failure both land on sign-in
