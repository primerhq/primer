"""Functions gateway: happy path, args 422, fn 404, error mapping.

Spec: 2026-08-08-services-design.md section 7.1. The roundtrip tests
execute a REAL sandboxed subprocess via LocalHardenedRunner (stdlib
python, no network), which is the point: the gateway must ride the same
hardened path the python toolsets do.
"""

from __future__ import annotations

import io
import tarfile

import pytest

ADD_FN = (
    "@primer_tool(timeout_seconds=10)\n"
    "async def add(a: int, b: int) -> int:\n"
    "    return a + b\n\n"
    "@primer_tool(timeout_seconds=10)\n"
    "async def boom() -> int:\n"
    "    raise ValueError('kapow')\n"
)


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _publish_fn_app(client, name="fnapp"):
    r = await client.post(
        "/v1/services", json={"name": name, "description": "fn app"}
    )
    assert r.status_code == 201, r.text
    s = r.json()
    r = await client.post(
        f"/v1/services/{s['id']}/versions",
        content=_tar({"index.html": b"x", "functions.py": ADD_FN.encode()}),
        headers={"content-type": "application/gzip"},
    )
    assert r.status_code == 201, r.text
    return s


@pytest.mark.asyncio
async def test_fn_roundtrip(client):
    await _publish_fn_app(client)
    r = await client.post(
        "/svc/fnapp/_gateway/functions/add", json={"a": 2, "b": 3}
    )
    assert r.status_code == 200, r.text
    assert r.json() == 5


@pytest.mark.asyncio
async def test_fn_unknown_404_args_422(client):
    await _publish_fn_app(client, "fnapp2")
    r = await client.post("/svc/fnapp2/_gateway/functions/nope", json={})
    assert r.status_code == 404
    r = await client.post(
        "/svc/fnapp2/_gateway/functions/add", json={"a": "NaN-ish", "b": []}
    )
    assert r.status_code == 422
    assert "schema" in r.text


@pytest.mark.asyncio
async def test_fn_non_object_body_422(client):
    await _publish_fn_app(client, "fnapp4")
    r = await client.post(
        "/svc/fnapp4/_gateway/functions/add",
        content=b"[1,2,3]",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_fn_error_maps_500_with_traceback_console(client):
    await _publish_fn_app(client, "fnapp3")
    r = await client.post("/svc/fnapp3/_gateway/functions/boom", json={})
    assert r.status_code == 500
    body = r.json()
    assert "kapow" in str(body)
    assert "traceback" in body  # viewer_auth=console includes it


@pytest.mark.asyncio
async def test_gateway_unknown_service_404(client):
    r = await client.post("/svc/ghost/_gateway/functions/add", json={})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_client_js_served(client):
    r = await client.get("/svc/_client/primer.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "Primer" in r.text and "_gateway/functions/" in r.text
