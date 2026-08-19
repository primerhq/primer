"""Classic Studio host adapter for client tools (S3 spec section 5)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
ADAPTER = UI / "components" / "studio" / "st-client-tools.jsx"
STUDIO = UI / "components" / "studio.jsx"
CENTER = UI / "components" / "studio-center.jsx"
INDEX = UI / "index.html"


def _adapter() -> str:
    return ADAPTER.read_text(encoding="utf-8")


def test_adapter_exists_and_exports() -> None:
    assert ADAPTER.exists()
    src = _adapter()
    assert "function ST_ClientTools(" in src
    assert "window.ST_ClientTools" in src
    assert 'src="components/studio/st-client-tools.jsx"' in INDEX.read_text(
        encoding="utf-8"
    )


def test_adapter_injects_the_three_host_callbacks() -> None:
    src = _adapter()
    assert "CT_createExecutor" in src
    for key in ("openDoc:", "toast:", "attachLifecycle:"):
        assert key in src, key


def test_open_doc_adapts_to_the_studio_open_tab_contract() -> None:
    src = _adapter()
    assert "st.openTab" in src
    assert '"file:" + ref' in src
    assert 'kind: "file"' in src
    assert "ref: ref" in src


def test_studio_open_tab_is_idempotent_on_tab_id() -> None:
    # The contract that makes multi-client duplicate delivery harmless
    # (spec section 7): a second openTab with the same id must not append
    # a second tab. Pinned HERE because the adapter relies on it.
    src = STUDIO.read_text(encoding="utf-8")
    assert "var exists = (s.openTabs || []).some(" in src
    assert "var openTabs = exists ? s.openTabs : s.openTabs.concat([tab]);" in src


def test_open_doc_carries_the_line_and_refreshes_it_on_reopen() -> None:
    # open_file(path, line) (spec section 5). openTab's id dedupe means an
    # ALREADY-OPEN tab keeps its old record, so a second open_file at a
    # different line has to patch the line onto the existing tab or the
    # viewer would never move.
    src = _adapter()
    assert "line: line || null" in src
    assert "st.patch(" in src
    assert "openTabs:" in src


def test_file_panel_scrolls_to_the_requested_line() -> None:
    # The other half of the same contract: FilePanel honours tab.line and
    # the code preview carries the anchors it scrolls to.
    src = CENTER.read_text(encoding="utf-8")
    assert 'data-line={i + 1}' in src
    assert "ST_ctLineRef" in src
    assert "scrollIntoView" in src
    assert '[data-line="' in src


def test_inform_binds_the_single_global_toast_entry_point() -> None:
    src = _adapter()
    assert "window.primerApi.toastPush" in src


def test_lifecycle_hits_the_attach_endpoints_and_heartbeats() -> None:
    src = _adapter()
    assert '"/workspaces/" + encodeURIComponent(wid)' in src
    assert "/attach" in src
    assert 'apiFetch("POST"' in src
    assert 'apiFetch("DELETE"' in src
    assert "ST_CT_HEARTBEAT_MS" in src
    assert "setInterval" in src


def test_adapter_reuses_the_shared_tap_and_opens_no_socket() -> None:
    src = _adapter()
    assert "useWorkspaceTapListener" in src
    assert "EventSource" not in src


def test_studio_mounts_the_adapter_with_the_active_session() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "window.ST_ClientTools" in src
    assert "sid={activeSessionId}" in src
    assert "studio={studio}" in src
