"""One file names URLs, so a route change has one edit site.

Mirrors the discipline of ui/components/studio/st-api.jsx:1-5, and pins
the two endpoints S8 is the FIRST console consumer of: the S1 binding
switch and the S7 turn timeline.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-api.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_in_the_bundle() -> None:
    assert 'src="components/shell/sh-api.jsx"' in (UI / "index.html").read_text(
        encoding="utf-8"
    )
    assert "window.SH_api = SH_api;" in _src()


def test_binding_switch_matches_the_s1_endpoint() -> None:
    """S1 spec section 6: POST /workspaces/{wid}/sessions/{sid}/binding."""
    src = _src()
    assert '"/binding"' in src
    assert re.search(r'switchBinding:\s*function\s*\(wid,\s*sid,\s*binding\)', src)
    assert '"POST"' in src


def test_trace_timeline_matches_the_s7_endpoint() -> None:
    """S7 spec section 5: GET /sessions/{sid}/turns/{turn_no}/timeline."""
    src = _src()
    assert '"/turns/"' in src and '"/timeline"' in src


def test_approval_decisions_use_the_shipped_respond_body() -> None:
    src = _src()
    assert '"/tool_approval/respond"' in src
    assert 'decision: "approved"' in src
    assert 'decision: "rejected"' in src


def test_no_other_shell_file_names_a_v1_url() -> None:
    """A second URL site is how st-api's discipline was worth having."""
    shell = UI / "components" / "shell"
    for path in sorted(shell.glob("*.jsx")):
        if path.name == "sh-api.jsx":
            continue
        body = path.read_text(encoding="utf-8")
        assert '"/workspaces/' not in body, path.name
        assert '"/sessions/' not in body, path.name


def test_every_read_takes_an_abort_signal() -> None:
    src = _src()
    for name in ("sessions", "session", "filesTree", "fileRead", "commitLog",
                 "commit", "pendingYields", "approvalRecords", "timeline"):
        m = re.search(name + r":\s*function\s*\(([^)]*)\)", src)
        assert m, name
        assert m.group(1).strip().endswith("signal"), name
