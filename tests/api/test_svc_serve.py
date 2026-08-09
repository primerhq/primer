"""Static serving: resolution, SPA fallback, caching, serve-only flag.

Spec: 2026-08-08-services-design.md section 6.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _publish_app(client, name="serveme"):
    r = await client.post(
        "/v1/services", json={"name": name, "description": "serving test"}
    )
    assert r.status_code == 201, r.text
    s = r.json()
    r = await client.post(
        f"/v1/services/{s['id']}/versions",
        content=_tar({
            "index.html": b"<h1>hello</h1>",
            "app.js": b"console.log(1)",
            "assets/logo.svg": b"<svg/>",
        }),
        headers={"content-type": "application/gzip"},
    )
    assert r.status_code == 201, r.text
    return s


@pytest.mark.asyncio
async def test_serves_entry_and_assets(client):
    await _publish_app(client)
    r = await client.get("/svc/serveme/")
    assert r.status_code == 200
    assert "hello" in r.text
    assert r.headers["content-type"].startswith("text/html")
    js = await client.get("/svc/serveme/app.js")
    assert js.status_code == 200
    assert "immutable" in js.headers["cache-control"]
    assert js.headers.get("etag")
    svg = await client.get("/svc/serveme/assets/logo.svg")
    assert svg.status_code == 200
    assert "svg" in svg.headers["content-type"]


@pytest.mark.asyncio
async def test_root_redirects_to_slash(client):
    await _publish_app(client, "redirected")
    r = await client.get("/svc/redirected", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/svc/redirected/"


@pytest.mark.asyncio
async def test_spa_fallback_and_asset_404(client):
    await _publish_app(client, "spa")
    deep = await client.get("/svc/spa/settings/profile")
    assert deep.status_code == 200 and "hello" in deep.text
    missing = await client.get("/svc/spa/missing.png")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_unknown_service_404(client):
    r = await client.get("/svc/never-created/")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_unpublished_404_html_and_json(client):
    await client.post(
        "/v1/services", json={"name": "empty", "description": "unpublished"}
    )
    html = await client.get("/svc/empty/", headers={"accept": "text/html"})
    assert html.status_code == 404 and "not published" in html.text.lower()
    js = await client.get("/svc/empty/", headers={"accept": "application/json"})
    assert js.status_code == 404
    assert js.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_etag_304(client):
    await _publish_app(client, "cached")
    first = await client.get("/svc/cached/app.js")
    again = await client.get(
        "/svc/cached/app.js", headers={"if-none-match": first.headers["etag"]}
    )
    assert again.status_code == 304


@pytest.mark.asyncio
async def test_rollback_switches_content(client):
    s = await _publish_app(client, "roll")
    r2 = await client.post(
        f"/v1/services/{s['id']}/versions",
        content=_tar({"index.html": b"<h1>two</h1>"}),
        headers={"content-type": "application/gzip"},
    )
    assert r2.status_code == 201, r2.text
    assert "two" in (await client.get("/svc/roll/")).text
    versions = (await client.get(f"/v1/services/{s['id']}/versions")).json()
    first_id = [v["id"] for v in versions["items"] if v["version"] == 1][0]
    await client.post(
        f"/v1/services/{s['id']}/_activate", json={"version_id": first_id}
    )
    assert "hello" in (await client.get("/svc/roll/")).text


def test_serve_only_app_has_svc_but_not_entity_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PRIMER_SERVE_ONLY", "1")
    from primer.api.app import create_app
    from primer.api.config import AppConfig

    app = create_app(AppConfig())
    # Included routers are lazily wrapped; walk one nesting level to
    # collect the real route paths.
    paths: set[str] = set()
    for route in app.routes:
        p = getattr(route, "path", None)
        if isinstance(p, str):
            paths.add(p)
        original = getattr(route, "original_router", None)
        if original is not None:
            ctx = getattr(route, "include_context", None)
            prefix = getattr(ctx, "prefix", "") if ctx is not None else ""
            for sub in original.routes:
                sp = getattr(sub, "path", None)
                if isinstance(sp, str):
                    paths.add(prefix + sp)
    assert any(p.startswith("/svc/") for p in paths)
    assert not any(p.startswith("/v1/agents") for p in paths)
    assert not any(p.startswith("/v1/services") for p in paths)
    assert any(p.startswith("/v1/health") for p in paths)
