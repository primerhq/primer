"""Bugs router — write-only POST."""

from __future__ import annotations

import base64
import json

import pytest


@pytest.mark.asyncio
async def test_post_writes_bug_directory(client, tmp_path, monkeypatch) -> None:
    """Happy path: directory + three files written, response carries id+path."""
    from primer.api.routers import bugs as _bugs_mod

    monkeypatch.setattr(
        _bugs_mod, "_resolve_bugs_dir", lambda cfg: tmp_path / "bugs",
    )

    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
    resp = await client.post(
        "/v1/bugs",
        json={
            "description": "Button doesn't work",
            "screenshot_b64": png,
            "page_url": "http://localhost/console/sessions",
            "viewport": {"width": 1920, "height": 1080},
            "captured_at": "2026-06-02T14:30:00Z",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("bug-")

    bug_dir = tmp_path / "bugs" / body["id"]
    assert bug_dir.exists()
    assert (bug_dir / "description.md").read_text(encoding="utf-8") == "Button doesn't work"
    assert (bug_dir / "screenshot.png").exists()
    meta = json.loads((bug_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "open"
    assert meta["page_url"] == "http://localhost/console/sessions"
    assert meta["viewport"] == {"width": 1920, "height": 1080}
    assert meta["captured_at"] == "2026-06-02T14:30:00Z"


@pytest.mark.asyncio
async def test_post_without_screenshot(client, tmp_path, monkeypatch) -> None:
    """A description-only report still writes description.md + meta.json."""
    from primer.api.routers import bugs as _bugs_mod

    monkeypatch.setattr(
        _bugs_mod, "_resolve_bugs_dir", lambda cfg: tmp_path / "bugs",
    )
    resp = await client.post(
        "/v1/bugs",
        json={"description": "Just text, no image"},
    )
    assert resp.status_code == 201, resp.text
    bug_dir = tmp_path / "bugs" / resp.json()["id"]
    assert (bug_dir / "description.md").exists()
    assert not (bug_dir / "screenshot.png").exists()


@pytest.mark.asyncio
async def test_post_strips_data_url_prefix(client, tmp_path, monkeypatch) -> None:
    """A canvas.toDataURL() payload starts with ``data:image/png;base64,``;
    the router must strip it before decoding."""
    from primer.api.routers import bugs as _bugs_mod

    monkeypatch.setattr(
        _bugs_mod, "_resolve_bugs_dir", lambda cfg: tmp_path / "bugs",
    )
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
    resp = await client.post(
        "/v1/bugs",
        json={
            "description": "x",
            "screenshot_b64": f"data:image/png;base64,{png}",
        },
    )
    assert resp.status_code == 201, resp.text
    img = (tmp_path / "bugs" / resp.json()["id"] / "screenshot.png").read_bytes()
    assert img.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_post_requires_description(client) -> None:
    """Empty description fails Pydantic min_length=1."""
    resp = await client.post("/v1/bugs", json={"description": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_unauthenticated_rejected(raw_client) -> None:
    """Without a session cookie, /v1/bugs rejects."""
    resp = await raw_client.post("/v1/bugs", json={"description": "x"})
    assert resp.status_code in (401, 403)
