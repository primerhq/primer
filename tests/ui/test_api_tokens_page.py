"""Static checks for API tokens page."""

from pathlib import Path

TOKENS = Path(__file__).resolve().parents[2] / "ui" / "components" / "api_tokens.jsx"
ADMIN = Path(__file__).resolve().parents[2] / "ui" / "components" / "console" / "nv-system.jsx"
APP = Path(__file__).resolve().parents[2] / "ui" / "app.jsx"


def _src(): return TOKENS.read_text()


def test_page_component_defined():
    src = _src()
    assert "ApiTokensPage" in src or "TokensPage" in src


def test_create_modal_posts():
    src = _src()
    assert "/v1/auth/tokens" in src or "/auth/tokens" in src


def test_plaintext_one_time_display():
    src = _src()
    assert "plaintext-display" in src or "plaintext" in src.lower()


def test_revoke_present():
    src = _src()
    assert "revoke" in src.lower()


def test_table_testid():
    assert "api-tokens-table" in _src()


def test_table_uses_shared_tbl_class():
    # Consistency sweep: the tokens table uses the console-wide shared .tbl /
    # .tbl-wrap styling (like agents/channels/…), not the old hand-rolled
    # className="table" + inline per-cell padding.
    src = _src()
    assert 'className="tbl-wrap"' in src
    assert 'className="tbl"' in src
    assert 'className="table"' not in src
    assert '"8px 12px"' not in src


def test_admin_overlay_has_tokens_section():
    src = ADMIN.read_text()
    assert '"apikeys"' in src


def test_the_admin_overlay_renders_the_tokens_page():
    """The console has no route table: the overlay host IS the wiring."""
    src = (Path(__file__).resolve().parents[2] / "ui" / "components"
           / "console" / "nv-system.jsx").read_text()
    assert "AT_ApiTokensPage" in src
