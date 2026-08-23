"""The Inbox: typed attention triage as a first-class tab (revamp
spec section 5).

The shell-level SH_AttentionEngine owns the data (cross-workspace
pending fan-out + approval records + triage) and renders interrupt
toasts; the Inbox doc is a view over shell.attentionRef.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
INBOX = (UI / "components" / "shell" / "sh-inbox-doc.jsx").read_text(
    encoding="utf-8")
RAIL = (UI / "components" / "shell" / "sh-rail.jsx").read_text(
    encoding="utf-8")
HOST = (UI / "components" / "shell" / "sh-doc-host.jsx").read_text(
    encoding="utf-8")
SHELL = (UI / "components" / "shell" / "sh-shell.jsx").read_text(
    encoding="utf-8")
URLJS = (UI / "foundation" / "shell-url.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")


def test_inbox_is_an_addressable_doc_kind():
    assert '"inbox"' in URLJS
    assert 'kind === "inbox"' in HOST
    assert "SH_InboxDoc" in HOST


def test_inbox_verb_and_chord():
    m = re.search(r'id: "inbox.open"[\s\S]{0,220}', HOST)
    assert m and "Open Inbox" in m.group(0) and "Ctrl+j" in m.group(0)


def test_engine_is_shell_level_and_fans_out_across_workspaces():
    assert "SH_AttentionEngine" in SHELL, "engine mounts at shell level"
    m = re.search(r"function SH_AttentionEngine[\s\S]{0,900}", RAIL)
    assert m and "SH_api.workspaces" in m.group(0), (
        "pending fans out over the workspaces list"
    )


def test_items_decide_inline_and_jump_cross_workspace():
    assert "SH_DecisionCard" in INBOX
    assert "SH_buildUrl" in INBOX, "Open jumps across workspaces by URL"
    assert "SH_TriageVerbs" in INBOX


def test_keyboard_triage():
    for key in ("ArrowDown", "ArrowUp", "Enter"):
        assert key in INBOX, key


def test_resolved_stays_queryable():
    assert "Show resolved" in INBOX


def test_script_tag_present():
    assert "sh-inbox-doc.jsx" in HTML
