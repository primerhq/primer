"""The workspace template editor marks the admin-only fields (security review 2026-10-08: AUTHZ-02, INJ-02, AUTHZ-05).

The server refuses a non-admin write of container ``extra_mounts``, the Kubernetes overlays and secret file sources with
403 ``forbidden_role`` (shown by the editor's error toast), and refuses a Kubernetes overlay outside its allowlist for
everyone. The editor says so up front: a note above each group, and hints that name the allowlist.

Static-source + bundle-build checks only (matching the rest of the ui/ suite - no React render).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = (UI / "components" / "workspaces" / "templates.jsx").read_text("utf-8")


def test_the_admin_only_note_is_shown_on_both_groups() -> None:
    assert "const WT_ADMIN_ONLY_NOTE =" in SRC
    assert SRC.count('data-testid="ws-template-admin-only-note"') == 2
    assert SRC.count("{WT_ADMIN_ONLY_NOTE}") == 2
    # Lead review of #473: a template that holds any of them is admin-only to edit at all.
    assert "only an admin can edit a template that has any of them set" in SRC


def test_each_gated_field_hint_says_admin_only() -> None:
    for label in ("extra_mounts", "extra_volumes", "extra_volume_mounts", "pod_overrides"):
        start = SRC.index(f'<WS_FieldRow label="{label}"')
        row = SRC[start:SRC.index(">", start)]
        assert "admin only" in row, label


def test_the_k8s_hints_name_the_allowlist() -> None:
    assert "emptyDir or configMap volumes only" in SRC
    assert "only shareProcessNamespace, dnsPolicy, restartPolicy, terminationGracePeriodSeconds" in SRC
    assert "any non-empty value is refused" in SRC


def test_the_files_hint_says_secret_sources_are_admin_only() -> None:
    assert "secret sources are admin only" in SRC


def test_bundle_transpiles_with_the_notes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    assert "WT_ADMIN_ONLY_NOTE" in body.decode("utf-8")
