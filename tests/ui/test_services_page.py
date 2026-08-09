"""Static checks for the Services console page (spec section 9)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_route_page_and_script() -> None:
    router = _read("foundation/router.js")
    assert '"/services"' in router
    assert '"/services/:id"' in router
    assert 'root === "services"' in _read("app.jsx")
    assert 'src="components/services.jsx"' in _read("index.html")
    assert '"Services"' in _read("components/chrome.jsx")


def test_page_conventions() -> None:
    src = _read("components/services.jsx")
    assert "window.SV_ServicesPage" in src and "window.SV_ServiceDetail" in src
    assert "useResource" in src and '"/services' in src
    assert "harness_id" in src  # managed banner
    assert "_activate" in src  # rollback wiring
    assert "viewer_auth" in src
    assert "confirm(" in src  # switching to anonymous confirms
    assert "publish_service" in src  # versions empty state names the tool


def test_open_app_links_to_svc() -> None:
    src = _read("components/services.jsx")
    assert '"/svc/"' in src
