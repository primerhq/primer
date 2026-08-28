"""The universal search bar (wiring plan P1 T5): mixed result kinds
over one verb registry, keyboard selection spanning every group,
transient state never in the URL.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (
    ROOT / "ui" / "components" / "console" / "nv-palette.jsx"
).read_text(encoding="utf-8")
API = (
    ROOT / "ui" / "components" / "shell" / "sh-api.jsx"
).read_text(encoding="utf-8")


def test_mixed_result_kinds():
    # Group order per implementer-notes.md 1.3: Verbs, Sessions, Files,
    # Platform (the group was named "Entities" before the uiv2 R1 delta).
    for group in ('"Verbs"', '"Sessions"', '"Files"', '"Platform"'):
        assert group in SRC, group
    assert "SH_rankVerbs" in SRC
    assert "allSessions" in SRC


def test_entity_lists_exist_on_the_seam():
    assert re.search(r"agents: function \(signal\)", API)
    assert re.search(r"graphs: function \(signal\)", API)


def test_keyboard_spans_all_groups():
    assert "ArrowDown" in SRC and "ArrowUp" in SRC
    assert "flat[selIdx].run()" in SRC, (
        "Enter runs from the flattened cross-group list"
    )


def test_cross_workspace_session_rows_use_the_combined_navigation():
    """F2/F3 (2026-08-29 UI review): this used to raw-assign
    location.hash (its own navigation outside con's markPush bookkeeping,
    and a preview-only open) - now it routes through con.openInWorkspace
    (one history entry) and promotes, matching the rail's own rows."""
    m = re.search(r'workspace_id !== con\.wid[\s\S]{0,300}', SRC)
    assert m
    assert "con.openInWorkspace(s.workspace_id" in m.group(0)
    assert "SH_buildUrl" not in m.group(0)
    assert 'con.promoteDoc("session:" + s.session_id)' in SRC


def test_transient_state_stays_out_of_the_url():
    for banned in ("paletteOpen", "q=", "query="):
        assert banned not in "".join(
            re.findall(r"SH_buildUrl\(\{([\s\S]*?)\}\)", SRC)
        ), banned


# ---------------------------------------------------------------------------
# F9 (2026-08-29 UI review): triggers/toolsets entity rows, a wiki source
# over the system collection, and empty-query recents.
# ---------------------------------------------------------------------------


def test_triggers_and_toolsets_entity_sources_exist():
    assert re.search(r"triggers: function \(signal\)", API)
    assert re.search(r"toolsets: function \(signal\)", API)
    assert "SH_api.triggers(signal)" in SRC
    assert "SH_api.toolsets(signal)" in SRC
    # Same platform-overlay pattern as the pre-existing agent/graph rows.
    assert 'tag: "trigger", nav: "triggers"' in SRC
    assert 'tag: "toolset", nav: "toolsets"' in SRC


def test_wiki_source_opens_a_wiki_tab():
    assert '"Wiki"' in SRC
    assert re.search(r"collectionDocuments: function \(cid, signal\)", API)
    assert "SH_api.collectionDocuments(\"system\", signal)" in SRC
    # doc: <slug> - NV_WikiDoc splits its slug on the first "/" into
    # (collection_id, path), so the ref must carry the collection prefix.
    m = re.search(r'kind: "wiki"[\s\S]{0,60}', SRC)
    assert m
    assert '"system/" + d.path' in m.group(0)


def test_empty_query_shows_recent_sessions():
    assert '"Recent"' in SRC
    start = SRC.index("} else {", SRC.index("if (q.trim())"))
    end = SRC.index("\n  }\n\n  var flat", start)
    body = SRC[start:end]
    assert "sessions.data" in body
    assert "last_activity_at" in body
    assert "NV_PALETTE_CAP" in body
    # Shares the same row-building closure as the searched Sessions group,
    # so a fix to one path can't silently drift from the other.
    assert body.count(".map(sessionRow)") == 1
    assert SRC.count(".map(sessionRow)") == 2


def test_empty_query_also_shows_recent_files_from_the_already_open_cache():
    """"(and files if cheap)" - files.data is already fetched whenever the
    palette is open (not gated on a query), so surfacing a few here is a
    client-side sort, never a second fetch."""
    start = SRC.index("} else {", SRC.index("if (q.trim())"))
    end = SRC.index("\n  }\n\n  var flat", start)
    body = SRC[start:end]
    assert "files.data" in body
    assert "mtime" in body
    assert "!f.is_dir" in body, "a directory row has no open action"
    assert body.count(".map(fileRow)") == 1
    assert SRC.count(".map(fileRow)") == 2


def test_recents_absent_once_a_query_is_typed():
    start = SRC.index("if (q.trim()) {")
    end = SRC.index("} else {", start)
    body = SRC[start:end]
    assert '"Recent"' not in body
    assert "recentFiles" not in body


# ---------------------------------------------------------------------------
# Round-two gate flake (palette2): every row shared one generic testid, so
# a BDD scenario could only find "the agent row" by matching its label text
# with .first - fragile the moment ranking, order, or a new source (like
# any of the F9 ones above) puts something else ahead of it. r.key was
# already unique per row; it just was not exposed to the DOM.
# ---------------------------------------------------------------------------


def test_rows_expose_their_unique_key_for_exact_targeting():
    assert 'data-row-key={r.key}' in SRC


def test_the_shared_row_testid_is_unchanged():
    """run_verb() in tests/ui_e2e/_shell_helpers.py does
    get_by_test_id("nv-palette-row").first on the query-narrowed set - an
    EXACT match. Repointing the testid itself to a per-row value would
    silently break every caller of that helper, so data-row-key must be a
    second attribute alongside it, not a replacement."""
    assert 'data-testid="nv-palette-row"' in SRC
