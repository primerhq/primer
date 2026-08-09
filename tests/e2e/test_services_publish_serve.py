"""End-to-end: publish a service, serve it, call a bundle function, roll back."""

from __future__ import annotations

import io
import tarfile

import pytest

FN = (
    "@primer_tool(timeout_seconds=10)\n"
    "async def double(n: int) -> int:\n"
    "    return n * 2\n"
)


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_service_lifecycle(client) -> None:
    created = await client.post(
        "/v1/services",
        json={"name": "e2e-app", "description": "e2e lifecycle app"},
    )
    assert created.status_code == 201, created.text
    s = created.json()
    try:
        r1 = await client.post(
            f"/v1/services/{s['id']}/versions",
            content=_tar({"index.html": b"<h1>one</h1>", "functions.py": FN.encode()}),
            headers={"content-type": "application/gzip"},
        )
        assert r1.status_code == 201, r1.text

        page = await client.get("/svc/e2e-app/")
        assert page.status_code == 200 and "one" in page.text

        fn = await client.post(
            "/svc/e2e-app/_gateway/functions/double", json={"n": 21}
        )
        assert fn.status_code == 200, fn.text
        assert fn.json() == 42

        r2 = await client.post(
            f"/v1/services/{s['id']}/versions",
            content=_tar({"index.html": b"<h1>two</h1>"}),
            headers={"content-type": "application/gzip"},
        )
        assert r2.status_code == 201, r2.text
        assert "two" in (await client.get("/svc/e2e-app/")).text

        versions = (await client.get(f"/v1/services/{s['id']}/versions")).json()
        v1_id = [v["id"] for v in versions["items"] if v["version"] == 1][0]
        rb = await client.post(
            f"/v1/services/{s['id']}/_activate", json={"version_id": v1_id}
        )
        assert rb.status_code == 200, rb.text
        assert "one" in (await client.get("/svc/e2e-app/")).text

        client_js = await client.get("/svc/_client/primer.js")
        assert client_js.status_code == 200
        assert "Primer" in client_js.text
    finally:
        await client.delete(f"/v1/services/{s['id']}")
