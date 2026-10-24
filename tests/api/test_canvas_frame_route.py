"""``GET /canvas/frame``: the static frame shell the console sandboxes the canvas in (pivot P5, Task 13; design 8.6).

Outside ``/v1`` and with no auth dependency: the console's navigation of its iframe here is same-site and carries the Lax session cookie,
which the route ignores (the shell is the same for everyone), and the requests made from the sandboxed, opaque-origin frame carry none. It carries
its own CSP, which is the canvas's only script control (audit G8-06, the lead's ruling 5 on S6), and it is never mounted in WORKER or
serve-only mode (the lead's ruling 6 on S6).
"""

from __future__ import annotations

import pytest

from primer.api._app_middleware import _UI_DIR
from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.canvas.frame import build_frame_shell, frame_csp
from primer.model.scheduler import InMemorySchedulerConfig, RuntimeMode, SchedulerProviderConfig, SchedulerProviderType
from tests.api.conftest import _FakeStorageProvider

DESIGN = (
    "sandbox allow-scripts allow-forms; default-src 'none'; script-src 'unsafe-inline' "
    "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com https://esm.sh; style-src 'unsafe-inline'; "
    "img-src https: data: blob:; media-src https: data: blob:; font-src data: https:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'"
)


async def test_the_frame_csp_is_the_designs(raw_client):                 # Review Focus 4
    shell = build_frame_shell(_UI_DIR)
    r = await raw_client.get(f"/canvas/frame?v={shell.version}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == frame_csp(own_origin="http://test")
    assert "img-src https: data: blob: http://test;" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.content == shell.body
    assert b"allow-same-origin" not in r.content


async def test_the_csp_header_is_the_only_policy_served(raw_client):
    """Ruling 5 on S6, the header half: exactly one policy reaches the browser, and it is the design's."""
    r = await raw_client.get("/canvas/frame")
    assert r.headers.get_list("content-security-policy") == [frame_csp(own_origin="http://test")]


def test_an_https_request_gets_the_exact_design_policy():
    assert frame_csp(own_origin=None) == DESIGN


async def test_a_stale_version_is_not_cached(raw_client):
    r = await raw_client.get("/canvas/frame?v=0000000000000000")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert (await raw_client.get("/canvas/frame")).headers["cache-control"] == "no-store"


async def test_the_frame_needs_no_session(raw_client):
    r = await raw_client.get("/canvas/frame")
    assert r.status_code == 200 and b"window.CV" in r.content


@pytest.mark.parametrize("host", ["evil.example; connect-src *", "a b", "[::1", ""])
async def test_a_host_that_is_not_one_adds_no_origin(raw_client, host):
    """The request's own origin joins img-src and media-src on plain http (Decision 11). A Host header that is not a host name cannot
    write into the policy: it adds nothing, so no directive is ever injected ahead of the design's."""
    r = await raw_client.get("/canvas/frame", headers={"host": host})
    assert r.headers["content-security-policy"] == DESIGN


async def test_a_host_with_a_port_is_the_origin(raw_client):
    r = await raw_client.get("/canvas/frame", headers={"host": "primer.local:9000"})
    assert "img-src https: data: blob: http://primer.local:9000;" in r.headers["content-security-policy"]


def _paths(app) -> set[str]:
    """Every route path of ``app``, the included routers' prefixed paths too (they are wrapped one level deep)."""
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            prefix = getattr(context, "prefix", "") if context is not None else ""
            paths.update(prefix + sub.path for sub in original.routes if isinstance(getattr(sub, "path", None), str))
    return paths


def test_the_public_canvas_routes_are_mounted_outside_v1(app):
    paths = _paths(app)
    assert "/canvas/frame" in paths
    assert not any(p.startswith("/v1/canvas/frame") for p in paths)


def test_a_worker_never_mounts_the_public_canvas_routes(monkeypatch):
    monkeypatch.setattr("primer.api.app._build_storage_provider", lambda _cfg: _FakeStorageProvider())
    app = create_app(AppConfig(
        runtime_mode=RuntimeMode.WORKER,
        scheduler=SchedulerProviderConfig(provider=SchedulerProviderType.IN_MEMORY, config=InMemorySchedulerConfig()),
    ))
    assert not any(p.startswith("/canvas") for p in _paths(app))


def test_a_serve_only_replica_never_mounts_the_public_canvas_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PRIMER_SERVE_ONLY", "1")
    app = create_app(AppConfig())
    paths = _paths(app)
    assert any(p.startswith("/svc/") for p in paths), "the serve-only app was built"
    assert not any(p.startswith("/canvas") for p in paths)


async def test_an_install_without_the_frame_answers_404(raw_client, tmp_path, monkeypatch):
    """Review of #908, round 2: no ``ui/`` (or a broken ``ui/canvas``) is a 404 on the frame route, not a 500."""
    from primer.api import _app_middleware

    monkeypatch.setattr(_app_middleware, "_UI_DIR", tmp_path / "no-ui")
    r = await raw_client.get("/canvas/frame")
    assert r.status_code == 404 and r.json()["detail"] == "This install has no canvas frame."
    (tmp_path / "broken" / "canvas").mkdir(parents=True)
    (tmp_path / "broken" / "canvas" / "frame.html").write_text("<!doctype html><title>no placeholders</title>", encoding="utf-8")
    monkeypatch.setattr(_app_middleware, "_UI_DIR", tmp_path / "broken")
    assert (await raw_client.get("/canvas/frame")).status_code == 404
