"""E2E: workspace lifecycle on the local backend.

Covers backlog items T0006, T0030, T0031, T0046, T0047, T0048, T0051.
All tests share the same Provider+Template+Workspace setup, so they
live in one module.

The local backend writes files into the host filesystem under the
provider's configured ``path``. Each test uses pytest's ``tmp_path``
fixture so the test gets a fresh, OS-managed workspace root that is
cleaned up automatically after the test exits.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest


def _provider_body(entity_id: str, root: Path) -> dict:
    return {
        "id": entity_id,
        "provider": "local",
        "config": {"kind": "local", "path": str(root)},
    }


def _template_body(entity_id: str, *, provider_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "test workspace template",
        "provider_id": provider_id,
        "backend": {"kind": "local"},
    }


def _workspace_body(*, template_id: str) -> dict:
    """Body for POST /v1/workspaces.

    NB: we deliberately omit ``id``. The local backend (and the
    container/k8s backends in general) ignore the user-supplied id
    and generate their own; if the API persists a user-supplied id
    on the row but the backend keeps the workspace under its own
    auto-id internally, subsequent file ops 404 on "backend has no
    live instance". This is a contract bug in the API layer that's
    been noted in 01-app-spec.md §12 for follow-up.
    """
    return {"template_id": template_id}


async def _setup_provider_template(
    client: httpx.AsyncClient,
    *,
    suffix: str,
    root: Path,
) -> tuple[str, str]:
    provider_id = f"wp-{suffix}"
    template_id = f"wt-{suffix}"
    pr = await client.post(
        "/v1/workspace_providers", json=_provider_body(provider_id, root),
    )
    assert pr.status_code == 201, pr.text
    tpl = await client.post(
        "/v1/workspace_templates",
        json=_template_body(template_id, provider_id=provider_id),
    )
    assert tpl.status_code == 201, tpl.text
    return provider_id, template_id


async def _teardown_provider_template(
    client: httpx.AsyncClient, provider_id: str, template_id: str,
) -> None:
    await client.delete(f"/v1/workspace_templates/{template_id}")
    await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0006 — full lifecycle: create provider+template+workspace, file ops, delete
# ============================================================================


@pytest.mark.asyncio
async def test_t0006_workspace_lifecycle_local_backend(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        # --- materialise the workspace (backend allocates the id)
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]
        assert workspace_id, ws.text

        # --- write a file
        path = "hello.txt"
        body = {"content": "hello world", "encoding": "text"}
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json=body,
        )
        assert write.status_code == 204, write.text

        # --- read it back
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path},
        )
        assert read.status_code == 200, read.text
        body_out = read.json()
        assert body_out["path"] == path
        assert body_out["content"] == "hello world"
        assert body_out["encoding"] == "text"
        assert body_out["size_bytes"] == 11

        # --- delete the file
        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
        )
        assert rm.status_code == 204, rm.text

        # --- delete the workspace
        rmws = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert rmws.status_code == 204, rmws.text
    finally:
        # Best-effort teardown — DELETE workspace will 404 if the test
        # already removed it, that's fine.
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0030 — write→read round-trip preserves bytes; delete→read = 404; list
# ============================================================================


@pytest.mark.asyncio
async def test_t0030_workspace_file_round_trip_and_delete(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # --- bytes round-trip via base64 (the exact-bytes contract)
        path = "data.bin"
        raw = bytes(range(256))  # 0..255 — every byte value
        encoded = base64.b64encode(raw).decode("ascii")
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": encoded, "encoding": "base64"},
        )
        assert write.status_code == 204, write.text

        # --- list reflects the write
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        assert path in names, f"{path!r} not in list: {names!r}"

        # --- read back as base64; content must match exactly
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path, "encoding": "base64"},
        )
        assert read.status_code == 200, read.text
        body_out = read.json()
        assert body_out["encoding"] == "base64"
        assert base64.b64decode(body_out["content"]) == raw
        assert body_out["size_bytes"] == 256

        # --- delete, then read = 404 with /errors/not-found
        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
        )
        assert rm.status_code == 204, rm.text

        gone = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path},
        )
        assert gone.status_code == 404, gone.text
        assert gone.json()["type"] == "/errors/not-found"

        # --- list no longer shows the deleted file
        listed_after = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
        )
        assert listed_after.status_code == 200, listed_after.text
        names_after = [item["path"] for item in listed_after.json()["items"]]
        assert path not in names_after, (
            f"{path!r} still in list after delete: {names_after!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0031 — Content-Disposition is sanitised (no header injection)
# ============================================================================


@pytest.mark.asyncio
async def test_t0031_workspace_download_content_disposition_sanitised(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Awkward filename: spaces + apostrophes. Apostrophes are NOT in
        # [A-Za-z0-9._\- ] so the sanitiser must strip them. Adding
        # CR/LF would also exercise injection-resistance, but pytest +
        # httpx + Starlette refuse such paths upfront, which is itself
        # a defence in depth that we don't need to re-test here.
        weird_path = "weird name's file.txt"
        body = {"content": "x", "encoding": "text"}
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": weird_path},
            json=body,
        )
        assert write.status_code == 204, write.text

        dl = await client.get(
            f"/v1/workspaces/{workspace_id}/files/download",
            params={"path": weird_path},
        )
        assert dl.status_code == 200, dl.text

        cd = dl.headers.get("content-disposition")
        assert cd is not None, "Content-Disposition header missing"

        # Header must contain BOTH the legacy `filename="..."` and the
        # RFC 5987 `filename*=UTF-8''...` form.
        assert "filename=" in cd, cd
        assert "filename*=UTF-8''" in cd, cd

        # No header injection: no raw CR or LF in the header value.
        assert "\r" not in cd and "\n" not in cd, repr(cd)

        # The legacy `filename=` value must be the sanitised slug —
        # only [A-Za-z0-9._\- ] survive; apostrophe is replaced by '_'.
        # The header is `attachment; filename="<slug>"; filename*=...`.
        legacy = cd.split('filename="', 1)[1].split('"', 1)[0]
        for ch in legacy:
            assert ch.isalnum() or ch in "._- ", (
                f"unsanitised char {ch!r} survived in filename={legacy!r}"
            )
        assert "'" not in legacy
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0051 — anomaly pin: WorkspaceCreateBody.id is silently ignored
# ============================================================================


@pytest.mark.asyncio
async def test_t0051_workspace_create_user_id_silently_ignored(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0051 — pin the documented anomaly in 01-app-spec.md §12.

    POSTing /v1/workspaces with `id="..."` causes the server to:
    - return 201 with that user id in the response body's id field, OR
    - return 201 with a backend-generated id (the local backend
      auto-generates one and ignores the user-supplied id).

    Either way, follow-on file ops keyed on the user-supplied id will
    404 because the in-memory backend cache is keyed on its own
    auto-generated id.

    This regression test pins the ANOMALOUS behaviour: the response
    id MUST equal the user-supplied id (so the API at least preserves
    it on the row), and a file PUT against the user-supplied id MUST
    404 with /errors/not-found pointing at the "row exists but the
    backend has no live instance" diagnostic. If a future fix wires
    the id through to the backend, this test will start failing and
    force the spec + this test to be updated together.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    user_supplied_id = f"ws-user-{unique_suffix}"
    backend_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces",
            json={"id": user_supplied_id, "template_id": template_id},
        )
        assert ws.status_code == 201, ws.text
        body = ws.json()
        # Anomaly: the row's id is the user-supplied one, but the live
        # backend instance is keyed under a different id.
        assert body["id"] == user_supplied_id, (
            f"anomaly broken — row no longer carries user-supplied id: {body!r}"
        )

        # File PUT keyed on the user-supplied id must 404 with the
        # documented "row exists but backend has no live instance"
        # diagnostic. If this assertion starts failing, the underlying
        # bug has been fixed (or partially fixed); update both this
        # test and the spec.
        write = await client.put(
            f"/v1/workspaces/{user_supplied_id}/files",
            params={"path": "x.txt"},
            json={"content": "noop", "encoding": "text"},
        )
        assert write.status_code == 404, (
            f"anomaly broken — file PUT on user id no longer 404s: "
            f"{write.status_code}: {write.text}"
        )
        assert "/errors/not-found" in write.text, write.text
        assert "row exists" in write.text or "live instance" in write.text, (
            f"diagnostic message changed; update this pin: {write.text}"
        )
    finally:
        # Best-effort: row delete by user id may itself be funky; try anyway.
        await client.delete(f"/v1/workspaces/{user_supplied_id}")
        if backend_id is not None:
            await client.delete(f"/v1/workspaces/{backend_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0046 — write to a nested path creates intermediate directories
# ============================================================================


@pytest.mark.asyncio
async def test_t0046_workspace_write_creates_parent_directories(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0046 — PUT /files at a path with non-existent parent dirs must
    materialise those parents (no 404 / 500 for missing intermediate
    directories) and the file must be readable back."""
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        nested = "a/b/c/file.txt"
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": nested},
            json={"content": "deep", "encoding": "text"},
        )
        assert write.status_code == 204, (
            f"PUT to nested path should auto-create parents, got "
            f"{write.status_code}: {write.text}"
        )

        # Read back — exact content
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": nested},
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == "deep"

        # Listing the deepest parent should show the file
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "a/b/c"},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        # Items use either the basename or the workspace-relative path —
        # check both for robustness against future formatting tweaks.
        assert any(
            name == "file.txt" or name.endswith("/file.txt") or name == nested
            for name in names
        ), f"file.txt not found in {names!r}"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0047 — 1 MiB binary content survives base64 round-trip exactly
# ============================================================================


@pytest.mark.asyncio
async def test_t0047_workspace_large_binary_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0047 — write 1 MiB random bytes via base64 encoding, read back,
    assert byte-for-byte equality. Pins the contract that the file API
    is a transparent byte conduit (no encoding-mangling, no surprise
    1 MiB cap)."""
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        path = "blob.bin"
        # 1 MiB of true randomness — exercises every byte value with
        # very high probability across the payload, so a regression
        # that special-cases certain bytes is exposed across runs.
        raw = os.urandom(1024 * 1024)
        encoded = base64.b64encode(raw).decode("ascii")

        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": encoded, "encoding": "base64"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        assert write.status_code == 204, write.text

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path, "encoding": "base64"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["encoding"] == "base64"
        assert body["size_bytes"] == len(raw)
        decoded = base64.b64decode(body["content"])
        assert len(decoded) == len(raw), (
            f"size mismatch: expected {len(raw)}, got {len(decoded)}"
        )
        assert decoded == raw, (
            "1 MiB round-trip differs; first divergent byte at index "
            f"{next((i for i, (a, b) in enumerate(zip(raw, decoded)) if a != b), -1)}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0048 — security headers present on the streaming download endpoint
# ============================================================================


_DOWNLOAD_SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-resource-policy": "same-origin",
}


@pytest.mark.asyncio
async def test_t0057_workspace_log_returns_documented_shape(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0057 — `GET /v1/workspaces/{id}/log` returns 200 with the
    documented `{commits: [...]}` envelope immediately after the
    workspace is materialised. The commit list may legitimately be
    empty on the local backend (the .state repo is initialised lazily
    by session activity, not by file writes — see backlog T0058).

    NB: T0058 was deferred — file writes through the user-files API
    do NOT commit to the workspace's .state repo. Growing the log
    requires session-driven state mutations that the harness can't
    produce without real LLM credentials.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        log = await client.get(f"/v1/workspaces/{workspace_id}/log")
        assert log.status_code == 200, log.text
        body = log.json()
        assert "commits" in body, body
        assert isinstance(body["commits"], list), body

        # Default limit is 50 (the handler's Query default); explicit
        # limit must be honoured too.
        log_capped = await client.get(
            f"/v1/workspaces/{workspace_id}/log",
            params={"limit": 5},
        )
        assert log_capped.status_code == 200, log_capped.text
        assert len(log_capped.json()["commits"]) <= 5
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0048_security_headers_on_streaming_download(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0048 — `GET /files/download` returns a StreamingResponse, and
    the security middleware must still attach all four §2 headers to
    streaming responses (a common middleware bug — some frameworks
    skip headers on streamed bodies)."""
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Seed a tiny file
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "small.txt"},
            json={"content": "hello", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        dl = await client.get(
            f"/v1/workspaces/{workspace_id}/files/download",
            params={"path": "small.txt"},
        )
        assert dl.status_code == 200, dl.text
        for name, expected in _DOWNLOAD_SECURITY_HEADERS.items():
            actual = dl.headers.get(name)
            assert actual == expected, (
                f"streaming response missing/incorrect header {name!r}: "
                f"expected {expected!r}, got {actual!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)
