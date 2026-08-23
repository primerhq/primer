"""Static checks for the Services console page (spec section 9)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_route_page_and_script() -> None:
    # The shell reaches page-shaped admin surfaces as overlays, addressed
    # by name in the URL hash rather than by a router table row.
    assert '"services"' in _read("foundation/shell-url.js")
    assert "services: {" in _read("components/console/nv-overlays.jsx")
    assert 'src="components/services.jsx"' in _read("index.html")


def test_page_conventions() -> None:
    src = _read("components/services.jsx")
    assert "window.SV_ServicesPage" in src and "window.SV_ServiceDetail" in src
    assert "useResource" in src and '"/services' in src
    assert "harness_id" in src  # managed banner
    assert "_activate" in src  # rollback wiring
    assert "viewer_auth" in src
    # Switching to anonymous confirms, through the console's own dialog
    # rather than the browser's.
    assert "confirmDialog(" in src
    assert "publish_service" in src  # versions empty state names the tool


def test_open_app_links_to_svc() -> None:
    src = _read("components/services.jsx")
    assert '"/svc/"' in src
