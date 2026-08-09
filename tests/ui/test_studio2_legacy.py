"""Static checks for legacy iframe documents (plan task 4)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_legacy_kind_and_iframe() -> None:
    src = _read("components/studio2/s2-legacy.jsx")
    assert 'registerKind("legacy"' in src
    assert "<iframe" in src


def test_legacy_route_table_covers_console_ia() -> None:
    src = _read("components/studio2/s2-legacy.jsx")
    for ref in ["/", "/agents", "/graphs", "/chats", "/workspaces",
                "/workspaces/templates", "/workspaces/providers",
                "/knowledge/collections", "/knowledge/documents",
                "/subsystems/internal-collections", "/ssp",
                "/providers/llm", "/model-profiles", "/providers/embedding",
                "/providers/cross_encoder", "/web-search",
                "/toolsets", "/tools", "/channels/channels",
                "/channels/rules", "/triggers", "/services", "/harnesses",
                "/workers", "/health", "/admin/users",
                "/admin/sso-providers", "/settings/api-tokens",
                "/settings/linked-accounts", "/settings/mcp", "/docs"]:
        assert f'"{ref}"' in src, f"legacy route table must cover {ref}"


def test_console_csp_permits_same_origin_framing() -> None:
    # The bridge only works if /console responses allow same-origin
    # frame ancestors; cross-origin must stay blocked.
    mw = (ROOT / "primer/api/_app_middleware.py").read_text(encoding="utf-8")
    assert "frame-ancestors 'self'" in mw
    assert "frame-ancestors 'none'" not in mw
    # default-src 'none' blocks embedding without an explicit frame-src.
    assert "frame-src 'self'" in mw
    assert '"SAMEORIGIN"' in mw
