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
    # uiv2 Wave 1: relabeled "Recent" -> "Sessions" to match the
    # mockup's own taxonomy (Verbs, Sessions, Files) - it already only
    # ever held sessions. Scoped to the else-branch body, not a bare
    # SRC-wide check, since "Sessions" also names the searched group
    # below (NV_matchRows(..., q, "Sessions")) for a different reason.
    start = SRC.index("} else {", SRC.index("if (q.trim())"))
    end = SRC.index("\n  }\n\n  var flat", start)
    body = SRC[start:end]
    assert 'label: "Sessions"' in body
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
    # uiv2 Wave 1: both branches now push a "Sessions" group (relabeled
    # from "Recent" to match the mockup taxonomy), so the two are no
    # longer told apart by label - check the empty-query-only recents
    # machinery (the capped/sorted variables) is absent here instead.
    start = SRC.index("if (q.trim()) {")
    end = SRC.index("} else {", start)
    body = SRC[start:end]
    assert "NV_PALETTE_CAP" not in body
    assert "last_activity_at" not in body
    assert "recentFiles" not in body
    assert 'label: "Sessions"' not in body


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


# ---------------------------------------------------------------------------
# uiv2 reconciliation Wave 1 (studio shell): group taxonomy, row
# iconography, FREQUENT tags, the self-referential "Open Palette" filter.
# Session/agent qualifier dedupe itself is a separate backend task
# (01a06431); this wave only builds the UI to render it once served.
# ---------------------------------------------------------------------------


def test_session_rows_carry_an_agent_glyph_not_a_text_badge():
    fn = SRC[SRC.index("function sessionRow("):SRC.index("function fileRow(")]
    assert "NV_identity(s.binding)" in fn
    assert "glyph: ident" in fn
    assert "tag: null" in fn


def test_file_and_verb_rows_lead_with_a_dot_not_a_text_badge():
    file_fn = SRC[SRC.index("function fileRow("):SRC.index("if (q.trim()) {")]
    assert "dot: true" in file_fn
    assert "tag: null" in file_fn
    assert 'f.is_dir ? "folder" : "file"' not in SRC

    verb_rows = SRC[SRC.index("var verbRows ="):SRC.index("if (verbRows.length)")]
    assert "dot: true" in verb_rows


def test_session_rows_carry_an_agent_at_workspace_sub_label():
    """Client-computable disambiguator for the triplicate-'main'-rows live
    bug (backend dedupe/qualifier work is a separate task, not this one) -
    workspace/agent are already on every session row (s.workspace_id,
    s.binding), so this does not wait on that batch landing."""
    fn = SRC[SRC.index("function sessionRow("):SRC.index("function fileRow(")]
    assert "agentLabel + \" @ \"" in fn
    assert "sub: " in fn


def test_frequent_tag_reflects_in_session_frecency_not_invented():
    assert 'con.frecency.scoreFor(verb.id) > 0 ? "frequent" : null' in SRC


def test_open_palette_filters_itself_from_its_own_results():
    m = re.search(r"var rankedVerbsVisible = rankedVerbs\.filter\(([\s\S]{0,120})", SRC)
    assert m
    assert 'verb.id !== "palette.open"' in m.group(1)
    assert "rankedVerbsVisible.slice(0, 8)" in SRC


def test_recents_endpoint_404_degrades_to_the_client_derived_fallback():
    """The approved recents endpoint (sh-api.jsx's SH_api.recentSessions)
    may land on a separate branch after this one - a 404 must fall back
    to the exact same allSessions()-derived computation the palette
    shipped with before that endpoint existed, not surface an error."""
    assert "SH_api.recentSessions(signal)" in SRC
    m = re.search(r"\.catch\(function \(err\) \{([\s\S]{0,120})", SRC)
    assert m
    assert 'err.status === 404' in m.group(1)
    assert "return null;" in m.group(1)

    empty_start = SRC.index("} else {", SRC.index("if (q.trim())"))
    empty_end = SRC.index("\n  }\n\n  var flat", empty_start)
    body = SRC[empty_start:empty_end]
    assert "recents.data && recents.data.items" in body
    assert ".map(sessionRowFromRecent)" in body
    assert ".map(sessionRow)" in body, "the fallback branch must still exist"


def test_recent_endpoint_rows_read_the_agreed_field_names_defensively():
    fn = SRC[SRC.index("function sessionRowFromRecent("):SRC.index("function fileRow(")]
    assert "r.workspace_name" in fn
    assert "r.graph_ref" in fn
    assert "r.agent_id" in fn
    # Never throws on an unexpected shape - always resolves to something
    # renderable, same default sessionRow() uses.
    assert '"operator"' in fn
