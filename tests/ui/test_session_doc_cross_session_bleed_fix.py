"""SEV fix: a new session's tab rendered ANOTHER session's transcript +
status line until a hard refresh.

Root cause chain: NV_SessionDoc mounted with no `key` at both of its
call sites (nv-studio.jsx's desktop tab dispatcher, nv-mobile-shell.jsx's
mobile chat screen) - switching a tab's active session re-rendered the
SAME component instance with new props instead of unmounting/remounting
it, so for one render the component's own useResource("history") hook
still held the PREVIOUS session's stale data. A REST-history seed effect
ran on that same stale render and fed the previous session's message
records into the NEW session's store. SS_apply's own cross-session guard
(session-store.js) never caught this because SessionMessageRecord
carries no session_id field of its own (implicit in the fetch URL), so
the guard's `frame.session_id != null` check was always false and never
rejected anything.

Fix: key NV_SessionDoc (and its file/diff/wiki siblings, same missing-key
pattern) by session/tab id so a switch is a real remount, plus a defense-
in-depth stamp of session_id onto REST-seeded records at both call sites
that feed SS_apply from a REST fetch, so the existing guard has real
data to check even if a similar un-keyed window ever reopens elsewhere.

Static-source checks only (the tests/ui suite convention).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _studio_src() -> str:
    return (UI / "components" / "console" / "nv-studio.jsx").read_text(encoding="utf-8")


def _mobile_src() -> str:
    return (UI / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")


def _doc_src() -> str:
    return (UI / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _store_src() -> str:
    return (UI / "foundation" / "session-store.js").read_text(encoding="utf-8")


# ---- Root fix: keyed remount on session/tab switch -------------------------


def test_desktop_session_doc_mount_is_keyed_by_tab_ref() -> None:
    src = _studio_src()
    assert "<window.NV_SessionDoc key={tab.ref} sid={tab.ref} />" in src


def test_sibling_doc_kinds_get_the_same_keyed_fix() -> None:
    """Same un-keyed-dispatcher pattern, same fix - file/diff/wiki docs
    were exposed to the identical class of bug even though only the
    session case was reported."""
    src = _studio_src()
    assert "<window.NV_FileDoc key={tab.ref} path={tab.ref} />" in src
    assert "<window.NV_DiffDoc key={tab.ref} sha={tab.ref} />" in src
    assert "<window.NV_WikiDoc key={tab.ref} slug={tab.ref} />" in src


def test_mobile_session_doc_mount_is_keyed_by_sid() -> None:
    src = _mobile_src()
    assert '<window.NV_SessionDoc key={sid} sid={sid} queueLabel="+Q" />' in src


# ---- Defense in depth: stamp session_id before SS_apply --------------------


def test_history_seed_effect_stamps_session_id_before_applying() -> None:
    """SessionMessageRecord has no session_id of its own - stamp it so
    SS_apply's cross-session guard has something real to check, rather
    than relying solely on the keyed-remount fix never regressing."""
    doc = _doc_src()
    assert "window.SS_apply(store, Object.assign({}, items[i], { session_id: sid }));" in doc
    # The effect must re-run if sid itself changes, not just the store
    # reference or the history data.
    effect_start = doc.index("React.useEffect(function () {\n    var items = (history.data")
    effect_end = doc.index("}, [", effect_start)
    effect_end = doc.index("]);", effect_end)
    dep_array = doc[effect_start:effect_end]
    assert "sid" in dep_array.split("[")[-1] or "[store, sid, history.data" in doc


def test_catchup_reconcile_also_stamps_session_id() -> None:
    """SS_catchUp's own SS_apply loop (session-store.js) is a second,
    separate REST-seed call site with the same gap - fixed the same way
    for uniform defense in depth even though this path is already
    sid-scoped by construction (fetch URL uses store.sid)."""
    store = _store_src()
    assert (
        "SS_apply(store, Object.assign({}, items[j], { session_id: store.sid }));"
        in store
    )


def test_ss_apply_guard_itself_is_unchanged() -> None:
    """The guard was never wrong - it just had nothing to check for
    session_id-less REST records. Confirm the fix is at the call sites,
    not a weakening of the guard itself."""
    store = _store_src()
    assert "if (frame.session_id != null && frame.session_id !== store.sid) return;" in store


def test_bundle_transpiles_with_the_cross_session_fix() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    for f in ("nv-studio.jsx", "nv-mobile-shell.jsx", "nv-session-doc.jsx"):
        assert f"/* === components/console/{f} === */" in text
