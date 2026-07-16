"""Regression: the workspace TEMPLATE create/edit modal must expose a
"Strict write locking" toggle wired to `strict_write_locking`
(`WorkspaceTemplate.strict_write_locking`, Task 9), matching the
`role="switch"` toggle idiom used elsewhere in this console
(SSO_Toggle / CH_Toggle / AG_Toggle).

Static-source + bundle-build checks only (matching the rest of the
ui/ suite - no React render).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = (UI / "components" / "workspaces" / "templates.jsx").read_text("utf-8")


def test_toggle_is_present_and_wired() -> None:
    assert 'role="switch"' in SRC or "SSO_Toggle" in SRC or "WT_Toggle" in SRC
    assert "strict_write_locking" in SRC
    assert 'data-testid="ws-template-strict-write-locking"' in SRC


def test_form_defaults_and_body_include_field() -> None:
    # _emptyForm seeds it false; submit() forwards it in the body.
    assert "strict_write_locking: false" in SRC
    assert "strict_write_locking: form.strict_write_locking" in SRC or \
        "strict_write_locking: !!form.strict_write_locking" in SRC


def test_from_template_reads_field() -> None:
    assert "strict_write_locking: !!t.strict_write_locking" in SRC or \
        "strict_write_locking: t.strict_write_locking" in SRC


def test_bundle_transpiles_with_templates() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/workspaces/templates.jsx === */" in text
