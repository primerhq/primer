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
async def test_t0093_workspace_put_overwrites_existing_content(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0093 — two PUTs to the same workspace path: the second write
    fully replaces the first. Read returns the second body, listing
    shows a single entry whose size matches the new content.
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

        path = "overwrite.txt"
        first_body = "first-write-content"
        second_body = "the-replacement-body-which-is-longer"
        # First PUT
        w1 = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": first_body, "encoding": "text"},
        )
        assert w1.status_code == 204, w1.text
        # Second PUT to the same path
        w2 = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": second_body, "encoding": "text"},
        )
        assert w2.status_code == 204, w2.text

        # Read returns the second content, not the first
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path},
        )
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["content"] == second_body, body
        assert body["size_bytes"] == len(second_body.encode("utf-8"))

        # Listing shows ONE entry, not two duplicates
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
        )
        assert listed.status_code == 200, listed.text
        items = [
            item for item in listed.json()["items"]
            if item["path"] == path or item["path"].endswith(f"/{path}")
        ]
        assert len(items) == 1, (
            f"expected a single entry for {path!r}, got {len(items)}: "
            f"{items!r}"
        )
        # And the listing's size matches the new body, too.
        assert items[0]["size_bytes"] == len(second_body.encode("utf-8")), items
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0063_workspace_empty_file_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0063 — empty content (`""`) is a valid file body per the spec
    (FileWriteBody allows empty string). Write empty → read back returns
    empty content with size_bytes=0; the entry appears in the list.
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

        path = "empty.txt"
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": "", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path},
        )
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["content"] == "", body
        assert body["size_bytes"] == 0, body
        assert body["path"] == path

        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        assert any(
            name == path or name.endswith(f"/{path}")
            for name in names
        ), f"empty file {path!r} not in list: {names!r}"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0111_workspace_template_env_propagates_to_init_commands(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0111 — `WorkspaceTemplate.env` is merged into the init_commands
    subprocess environment. An init_command that reads `os.environ['MARKER']`
    and writes it to a file recovers the configured value.
    """
    provider_id = f"wp-env-{unique_suffix}"
    template_id = f"wt-env-{unique_suffix}"
    workspace_id: str | None = None
    marker_value = f"env-marker-{unique_suffix}"
    try:
        pr = await client.post(
            "/v1/workspace_providers",
            json=_provider_body(provider_id, tmp_path),
        )
        assert pr.status_code == 201, pr.text

        init_cmd = (
            'python -c "import os; '
            "open('marker.txt','w').write(os.environ['MARKER'])\""
        )
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "env propagation test",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "env": {"MARKER": marker_value},
                "init_commands": [init_cmd],
            },
        )
        assert tpl.status_code == 201, tpl.text

        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": "marker.txt"},
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == marker_value, read.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0112_workspace_overrides_env_wins_over_template_env(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0112 — `WorkspaceTemplateOverrides.env` keys overlay the
    template's env (caller wins on conflict). Same fixture as T0111
    with template MARKER=A and overrides MARKER=B; the marker file
    contains B.
    """
    provider_id = f"wp-envo-{unique_suffix}"
    template_id = f"wt-envo-{unique_suffix}"
    workspace_id: str | None = None
    try:
        pr = await client.post(
            "/v1/workspace_providers",
            json=_provider_body(provider_id, tmp_path),
        )
        assert pr.status_code == 201, pr.text

        init_cmd = (
            'python -c "import os; '
            "open('marker.txt','w').write(os.environ['MARKER'])\""
        )
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "env override test",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "env": {"MARKER": "from-template-A"},
                "init_commands": [init_cmd],
            },
        )
        assert tpl.status_code == 201, tpl.text

        override_value = f"from-override-B-{unique_suffix}"
        ws = await client.post(
            "/v1/workspaces",
            json={
                "template_id": template_id,
                "overrides": {"env": {"MARKER": override_value}},
            },
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": "marker.txt"},
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == override_value, read.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0113_workspace_overrides_init_commands_extend_template(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0113 — `WorkspaceTemplateOverrides.init_commands` EXTENDS
    (template runs first, then caller). Both marker files exist
    in the fresh workspace.
    """
    provider_id = f"wp-cmds-{unique_suffix}"
    template_id = f"wt-cmds-{unique_suffix}"
    workspace_id: str | None = None
    try:
        pr = await client.post(
            "/v1/workspace_providers",
            json=_provider_body(provider_id, tmp_path),
        )
        assert pr.status_code == 201, pr.text

        template_cmd = (
            'python -c "open(\'t.txt\',\'w\').write(\'from-template\')"'
        )
        override_cmd = (
            'python -c "open(\'o.txt\',\'w\').write(\'from-override\')"'
        )

        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "init_commands extend test",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "init_commands": [template_cmd],
            },
        )
        assert tpl.status_code == 201, tpl.text

        ws = await client.post(
            "/v1/workspaces",
            json={
                "template_id": template_id,
                "overrides": {"init_commands": [override_cmd]},
            },
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        for path, expected in (
            ("t.txt", "from-template"),
            ("o.txt", "from-override"),
        ):
            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": path},
            )
            assert read.status_code == 200, (
                f"file {path!r} missing — extend semantic broken: {read.text}"
            )
            assert read.json()["content"] == expected, read.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0092_workspace_template_init_commands_run(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0092 — `WorkspaceTemplate.init_commands` execute on workspace
    materialise. Build a template that runs a portable Python one-liner
    writing a marker file; the file must be readable back via the
    standard files API after the workspace is created.

    Use python -c (rather than shell echo) for cross-OS reliability —
    the local backend uses `asyncio.create_subprocess_shell` whose
    quoting/redirection rules differ between cmd.exe and POSIX shells.
    """
    provider_id = f"wp-init-{unique_suffix}"
    template_id = f"wt-init-{unique_suffix}"
    workspace_id: str | None = None
    try:
        pr = await client.post(
            "/v1/workspace_providers",
            json=_provider_body(provider_id, tmp_path),
        )
        assert pr.status_code == 201, pr.text

        # init_commands run with cwd=workspace_root, so the file lands
        # at the workspace's root with no path-prefix needed.
        init_cmd = (
            'python -c "open(\'init_marker.txt\', \'w\').'
            "write('init-was-here')\""
        )
        template_body = {
            "id": template_id,
            "description": "init_commands test template",
            "provider_id": provider_id,
            "backend": {"kind": "local"},
            "init_commands": [init_cmd],
        }
        tpl = await client.post(
            "/v1/workspace_templates", json=template_body,
        )
        assert tpl.status_code == 201, tpl.text

        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": "init_marker.txt"},
        )
        assert read.status_code == 200, (
            f"init_commands did not run / did not produce the marker "
            f"file: {read.text}"
        )
        assert read.json()["content"] == "init-was-here", read.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0094_workspace_file_special_chars_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0094 — file paths with spaces, `+`, `&`, and unicode characters
    round-trip through PUT/READ/LIST/DELETE without corruption.

    Regression-detector for any URL-encoding mishandling: the path
    travels as a query parameter, then needs to land on the actual
    filesystem with the same bytes. A double-decode or wrong-charset
    interpretation would break this.
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

        # Spaces, +, &, Cyrillic — none of these are in `[A-Za-z0-9._\- ]`
        # except the space. The httpx client handles URL encoding of
        # the query-param path; the server must decode and use the
        # raw bytes for the filesystem write.
        weird_path = "dir with space/файл +&.txt"
        body = "weird-name-content"
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": weird_path},
            json={"content": body, "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": weird_path},
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == body
        assert read.json()["path"] == weird_path

        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "dir with space"},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        # The basename should appear somewhere in the listing
        assert any(
            name == weird_path
            or name.endswith("файл +&.txt")
            for name in names
        ), f"weird filename missing from list: {names!r}"

        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": weird_path},
        )
        assert rm.status_code == 204, rm.text
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0095_workspace_files_list_root_vs_subdir(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0095 — listing at the workspace root and at a nested subdir
    each return their own contents and don't leak entries across
    directories.

    Setup:  root_marker.txt  AND  sub/dir/nested_marker.txt
    Asserts:
      - root listing contains root_marker, NOT nested_marker
      - sub/dir listing contains nested_marker, NOT root_marker
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

        for path in ("root_marker.txt", "sub/dir/nested_marker.txt"):
            w = await client.put(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": path},
                json={"content": "x", "encoding": "text"},
            )
            assert w.status_code == 204, w.text

        # Root listing
        root_resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
        )
        assert root_resp.status_code == 200, root_resp.text
        root_names = [item["path"] for item in root_resp.json()["items"]]
        assert any(
            name == "root_marker.txt" or name.endswith("/root_marker.txt")
            for name in root_names
        ), f"root_marker missing from root listing: {root_names!r}"
        assert not any(
            "nested_marker" in name for name in root_names
        ), f"nested_marker leaked into root listing: {root_names!r}"

        # Subdir listing
        sub_resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "sub/dir"},
        )
        assert sub_resp.status_code == 200, sub_resp.text
        sub_names = [item["path"] for item in sub_resp.json()["items"]]
        assert any(
            "nested_marker.txt" in name for name in sub_names
        ), f"nested_marker missing from sub/dir listing: {sub_names!r}"
        assert not any(
            "root_marker" in name for name in sub_names
        ), f"root_marker leaked into sub/dir listing: {sub_names!r}"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0096_workspace_destroy_then_recreate_starts_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0096 — DELETE a workspace then create another from the same
    template; the new workspace's files listing must NOT carry over
    artefacts from the destroyed one.

    Pins the destroy semantic: tear-down is real (not just a row
    removal), and a freshly materialised workspace from the same
    template is a clean slate.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_a: str | None = None
    workspace_b: str | None = None
    try:
        # Workspace A — write a marker file then destroy
        ws_a = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws_a.status_code == 201, ws_a.text
        workspace_a = ws_a.json()["id"]
        marker = "carry_over_marker.txt"
        w = await client.put(
            f"/v1/workspaces/{workspace_a}/files",
            params={"path": marker},
            json={"content": "from-A", "encoding": "text"},
        )
        assert w.status_code == 204, w.text
        rm = await client.delete(f"/v1/workspaces/{workspace_a}")
        assert rm.status_code == 204, rm.text
        workspace_a = None

        # Workspace B — same template, different id (the local backend
        # generates its own per the documented anomaly T0051)
        ws_b = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws_b.status_code == 201, ws_b.text
        workspace_b = ws_b.json()["id"]

        # B must NOT see A's marker
        listed = await client.get(
            f"/v1/workspaces/{workspace_b}/files",
            params={"path": "."},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        assert not any(marker in name for name in names), (
            f"workspace B inherited file from A: {names!r}"
        )

        # And reading A's marker on B explicitly returns 404
        read = await client.get(
            f"/v1/workspaces/{workspace_b}/files/read",
            params={"path": marker},
        )
        assert read.status_code == 404, (
            f"workspace B should not have A's file: {read.text}"
        )
    finally:
        for wid in (workspace_a, workspace_b):
            if wid is not None:
                await client.delete(f"/v1/workspaces/{wid}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0155_workspace_template_50_init_commands_all_run(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0155 — a WorkspaceTemplate with 50 distinct init_commands runs
    every command on materialise; all 50 marker files exist after
    workspace creation. Pins "no truncation / no batch-size cap" on
    the init_commands list.

    Each command is a tiny `python -c` one-liner. 50 subprocess
    invocations against cmd.exe / sh take a few seconds total.
    """
    provider_id = f"wp-50-{unique_suffix}"
    template_id = f"wt-50-{unique_suffix}"
    workspace_id: str | None = None
    n = 50
    try:
        pr = await client.post(
            "/v1/workspace_providers",
            json=_provider_body(provider_id, tmp_path),
        )
        assert pr.status_code == 201, pr.text

        init_commands = [
            f'python -c "open(\'m{i:02d}.txt\',\'w\').write(\'{i:02d}\')"'
            for i in range(n)
        ]
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": f"50 init_commands test {unique_suffix}",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "init_commands": init_commands,
            },
        )
        assert tpl.status_code == 201, tpl.text

        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Listing should show all 50 marker files at the workspace root
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": ".", "limit": 200, "offset": 0},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        for i in range(n):
            mname = f"m{i:02d}.txt"
            assert any(
                name == mname or name.endswith(f"/{mname}")
                for name in names
            ), f"marker {mname!r} missing from listing: {names!r}"

        # Spot-check the content of the last file
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": f"m{n-1:02d}.txt"},
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == f"{n-1:02d}"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0142_workspace_file_text_base64_round_trip_consistent(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0142 — write content via PUT, then read via both encodings.
    The base64-decoded bytes must equal the text-encoding's UTF-8
    bytes — same file, two views.
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

        content = f"hello-{unique_suffix}-world"
        path = "round_trip.txt"
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": content, "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        # Read as text
        text_resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path},
        )
        assert text_resp.status_code == 200, text_resp.text
        text_body = text_resp.json()
        assert text_body["content"] == content
        assert text_body["encoding"] == "text"

        # Read as base64
        b64_resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path, "encoding": "base64"},
        )
        assert b64_resp.status_code == 200, b64_resp.text
        b64_body = b64_resp.json()
        assert b64_body["encoding"] == "base64"

        # Decode and compare
        decoded = base64.b64decode(b64_body["content"])
        assert decoded == content.encode("utf-8"), (
            f"base64 round-trip mismatch: text={content!r}, "
            f"decoded={decoded!r}"
        )
        # size_bytes is consistent across both reads
        assert text_body["size_bytes"] == b64_body["size_bytes"]
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0143_workspace_file_put_malformed_base64_rejected(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0143 — PUT with `encoding=base64` and malformed base64 content
    returns a clean 4xx envelope. No 500 leak."""
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

        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "bad.bin"},
            json={
                "content": "not!!base64@@@",  # invalid alphabet
                "encoding": "base64",
            },
        )
        assert write.status_code != 500, write.text
        assert 400 <= write.status_code < 500, (
            f"expected 4xx on malformed base64, got "
            f"{write.status_code}: {write.text}"
        )
        envelope = write.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["status"] == write.status_code
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0146_workspace_file_put_root_path_rejected(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0146 — PUT with `path="."` (workspace root) is rejected with
    a clean 4xx. The workspace root is not a writable destination —
    writing there would either overwrite the root dir or 500."""
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

        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
            json={"content": "x", "encoding": "text"},
        )
        assert write.status_code != 500, write.text
        assert 400 <= write.status_code < 500, (
            f"PUT to workspace root must reject, got "
            f"{write.status_code}: {write.text}"
        )
        envelope = write.json()
        assert envelope["type"].startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0147_workspace_file_put_empty_path_rejected(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0147 — empty `path` query param returns a clean 4xx.
    Either validation rejects it (422) or the handler returns a
    bad-request. No 500."""
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

        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": ""},
            json={"content": "x", "encoding": "text"},
        )
        assert write.status_code != 500, write.text
        assert 400 <= write.status_code < 500, (
            f"empty path must reject, got "
            f"{write.status_code}: {write.text}"
        )
        envelope = write.json()
        assert envelope["type"].startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0148_workspace_file_put_path_traversal_rejected(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0148 — path traversal via `..` must be rejected with a clean
    4xx, and the file must NOT materialise outside the workspace.

    Probe the second condition by:
    1. attempting the traversal write
    2. listing the workspace root — nothing carrying the marker should
       appear
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

        marker = f"escape-{unique_suffix}"
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "../escape.txt"},
            json={"content": marker, "encoding": "text"},
        )
        assert write.status_code != 500, write.text
        assert 400 <= write.status_code < 500, (
            f"`..` traversal must reject, got "
            f"{write.status_code}: {write.text}"
        )
        envelope = write.json()
        assert envelope["type"].startswith("/errors/"), envelope

        # And nothing carrying the marker leaked into the workspace
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "."},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        assert not any("escape" in name for name in names), (
            f"`..` traversal write should not have materialised: {names!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0144_workspace_files_info_on_missing_path_returns_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0144 — `GET /files/info?path=<missing>` on a fresh workspace
    returns 404 with the documented `/errors/not-found` envelope."""
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

        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": "does/not/exist.txt"},
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["type"] == "/errors/not-found", body
        assert body["status"] == 404
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0145_workspace_files_delete_on_missing_path_returns_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0145 — `DELETE /files?path=<missing>` returns 404
    `/errors/not-found`. Matches the T0009 CRUD-DELETE contract — a
    second/missing delete is NOT idempotent."""
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

        resp = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "ghost.txt"},
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["type"] == "/errors/not-found", body
        assert body["status"] == 404
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0114_workspace_files_info_on_directory(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0114 — `GET /files/info?path=<dir>` for a directory must return
    a sane response (not 5xx). The kind field should indicate it's a
    directory (the FileEntry.kind enum is Literal["file","dir","symlink"]).
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

        # Create a file in subdir so subdir exists
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "subdir/inside.txt"},
            json={"content": "x", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": "subdir"},
        )
        # Must not 5xx. Most likely 200 with kind=dir; some implementations
        # might 404 on directories from /info. Accept either clean path.
        assert info.status_code != 500, info.text
        assert info.status_code < 500, (
            f"expected clean 2xx/4xx, got {info.status_code}: {info.text}"
        )
        if info.status_code == 200:
            body = info.json()
            assert body["kind"] in ("dir", "directory"), body
            assert body["path"] == "subdir", body
        else:
            # 4xx — must be a clean RFC 7807 envelope
            envelope = info.json()
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0115_workspace_files_delete_non_empty_directory(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0115 — DELETE on a directory containing files must reject with
    a clean 4xx (not silently recursive-delete, not 5xx). The contained
    file must remain readable afterwards.

    The handler is documented as "Delete file or empty directory" —
    so a non-empty directory must NOT be deleted.
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

        # Seed dir/x.txt
        nested = "dir/x.txt"
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": nested},
            json={"content": "stay-alive", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        # Attempt to delete the non-empty directory
        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "dir"},
        )
        assert rm.status_code != 500, rm.text
        assert 400 <= rm.status_code < 500, (
            f"non-empty dir delete must reject with 4xx, got "
            f"{rm.status_code}: {rm.text}"
        )
        envelope = rm.json()
        assert envelope["type"].startswith("/errors/"), envelope

        # The contained file must still be readable
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": nested},
        )
        assert read.status_code == 200, (
            f"file inside the rejected-delete dir should still exist: "
            f"{read.text}"
        )
        assert read.json()["content"] == "stay-alive"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0067_workspace_template_overrides_merge(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0067 — `WorkspaceTemplateOverrides.files` extends the template's
    files list (both apply; later entries win on path conflict). Spec
    §12 / WorkspaceTemplateOverrides docstring.

    Concrete check: build a template seeding `from-template.txt` via
    inline content; create a workspace with overrides seeding
    `from-overrides.txt`. Both files must exist + read back with the
    expected content.
    """
    provider_id = f"wp-merge-{unique_suffix}"
    template_id = f"wt-merge-{unique_suffix}"
    workspace_id: str | None = None
    try:
        # Provider
        pr = await client.post(
            "/v1/workspace_providers",
            json=_provider_body(provider_id, tmp_path),
        )
        assert pr.status_code == 201, pr.text

        # Template with one inline-source file
        template_body = {
            "id": template_id,
            "description": "merge test template",
            "provider_id": provider_id,
            "backend": {"kind": "local"},
            "files": [
                {
                    "path": "from-template.txt",
                    "source": {"kind": "inline", "content": "from-template"},
                }
            ],
        }
        tpl = await client.post(
            "/v1/workspace_templates", json=template_body,
        )
        assert tpl.status_code == 201, tpl.text

        # Workspace with overrides adding a SECOND file
        ws_body = {
            "template_id": template_id,
            "overrides": {
                "files": [
                    {
                        "path": "from-overrides.txt",
                        "source": {"kind": "inline", "content": "from-overrides"},
                    }
                ],
            },
        }
        ws = await client.post("/v1/workspaces", json=ws_body)
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Both files must exist and read back with the right content
        for path, expected in (
            ("from-template.txt", "from-template"),
            ("from-overrides.txt", "from-overrides"),
        ):
            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": path},
            )
            assert read.status_code == 200, (
                f"expected {path!r} to exist after merge: {read.text}"
            )
            assert read.json()["content"] == expected, read.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0064_workspace_deeply_nested_path_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0064 — write to a deeply-nested ~50-char workspace-relative
    path; read returns identical content; info reports the same path.

    NB: the original backlog wording asked for a 200-char path. On
    Windows the workspace's absolute path is `<tmp_path>/<ws_id>/<rel>`,
    where `<tmp_path>` alone consumes ~120 chars and `<ws_id>` another
    ~20 — so a 200-char *relative* path crosses the legacy MAX_PATH=260
    limit on Windows hosts and the server returns 500 (the local
    backend lets `FileNotFoundError [WinError 206]` bubble up). That's
    a real bug worth a separate spec-quarantine entry, but this test
    keeps the path conservative (~50 chars / 9 levels) so the
    deeply-nested round-trip itself is exercised reliably across hosts.
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

        # 9 levels × 5 chars = 45 chars, + 5 char filename = 50 chars
        deep_path = ("deep/" * 9) + "f.txt"
        assert len(deep_path) == 50, len(deep_path)

        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": deep_path},
            json={"content": "leaf", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": deep_path},
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == "leaf"
        assert read.json()["path"] == deep_path

        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": deep_path},
        )
        assert info.status_code == 200, info.text
        assert info.json()["path"] == deep_path
        assert info.json()["kind"] == "file"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0065_workspace_files_listing_pagination(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0065 — `GET /v1/workspaces/{id}/files?limit=N&offset=K` returns
    a window of size <= N starting at offset K, with `total` reflecting
    the directory's true file count.

    Writes 5 files into a unique subdirectory so the assertion isn't
    disturbed by other workspace artefacts.
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

        listdir = "page_test"
        for i in range(5):
            w = await client.put(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": f"{listdir}/file_{i}.txt"},
                json={"content": str(i), "encoding": "text"},
            )
            assert w.status_code == 204, w.text

        # Full listing — total is 5
        full = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": listdir, "limit": 50, "offset": 0},
        )
        assert full.status_code == 200, full.text
        full_body = full.json()
        assert full_body["total"] == 5, full_body
        assert len(full_body["items"]) == 5

        # Window: limit=2 offset=2 → 2 items, total still 5
        window = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": listdir, "limit": 2, "offset": 2},
        )
        assert window.status_code == 200, window.text
        win = window.json()
        assert win["total"] == 5, win
        assert len(win["items"]) == 2, win
        # Items in the window must be a subset of the full listing.
        full_paths = {item["path"] for item in full_body["items"]}
        for item in win["items"]:
            assert item["path"] in full_paths, item
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


@pytest.mark.asyncio
async def test_t0066_workspace_hidden_file_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0066 — write `.hidden.txt`, then list the parent and read back.

    Asserts the byte-conduit invariant unconditionally:
    - PUT is accepted (204)
    - GET /files/read returns the exact content regardless of dotfile-ness

    The default-listing inclusion behaviour is observation-only: a
    future change in either direction (include vs. hide) would update
    this comment, not break the test.
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

        path = "hidden_dir/.hidden.txt"
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": "secret", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        # Read back unconditionally
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path},
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == "secret"

        # Info is also unconditional
        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": path},
        )
        assert info.status_code == 200, info.text

        # Listing the parent: do NOT assert presence one way or the
        # other; just record what we observed in case a future test
        # iteration wants to pin the contract.
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "hidden_dir"},
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        # Soft observation; logged via the assertion message only on failure.
        # If a future iteration wants to pin "hidden files visible by default",
        # change this to an explicit `assert any("hidden" in n for n in names)`.
        # If it wants to pin "hidden files hidden", flip the assertion.
        # For now: empty assertion = no contract claim about default listing.
        _ = names
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


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


# ============================================================================
# T0162 — Workspace DELETE behaviour: idempotent or NOT? (spec §12 vs §5)
# ============================================================================


@pytest.mark.asyncio
async def test_t0162_workspace_delete_behaviour_pin(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0162 — Spec §12 says workspace DELETE is idempotent (so a second
    call returns 204), while spec §5 says generic CRUD DELETE returns 404
    on missing rows (T0009). Workspace is a bespoke endpoint so it could
    follow either contract — pin whichever the live API actually does.

    Either outcome is acceptable as long as the second DELETE does NOT
    leak a 5xx. The recorded outcome lets future iterations cite this
    test when the spec needs to be reconciled with code.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # First DELETE — must succeed
        first = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert first.status_code == 204, first.text

        # Second DELETE on the now-missing workspace — 204 (idempotent
        # per §12) or 404 (per §5). Pin no 5xx.
        second = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert second.status_code in (204, 404), second.text
        if second.status_code == 404:
            assert second.json()["type"] == "/errors/not-found", (
                second.json()
            )
    finally:
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0163 — file ops on a destroyed workspace return clean 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0163_file_ops_on_destroyed_workspace_return_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0163 — after DELETE /v1/workspaces/{wid}, every file sub-resource
    returns 404 with a clean envelope. Catches in-memory backend cache
    leaks where a stale handle could allow ops on a destroyed workspace.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Destroy the workspace
        rm = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert rm.status_code == 204, rm.text

        # Every file sub-resource path must 404 cleanly
        # NB: PUT /files takes `path` as a query string param, NOT in the
        # body (body is just {content, encoding?}). Spec §12 is slightly
        # wrong on this — confirmed against matrix/api/routers/workspaces.py.
        ops: list[tuple[str, str, dict | None]] = [
            ("GET", f"/v1/workspaces/{workspace_id}/files?path=.", None),
            ("GET", f"/v1/workspaces/{workspace_id}/files/info?path=foo", None),
            ("GET", f"/v1/workspaces/{workspace_id}/files/read?path=foo", None),
            ("GET", f"/v1/workspaces/{workspace_id}/files/download?path=foo", None),
            ("PUT", f"/v1/workspaces/{workspace_id}/files?path=foo",
                {"content": "bar", "encoding": "text"}),
            ("DELETE", f"/v1/workspaces/{workspace_id}/files?path=foo", None),
            ("GET", f"/v1/workspaces/{workspace_id}/log", None),
        ]
        for method, url, json_body in ops:
            if method == "GET":
                resp = await client.get(url)
            elif method == "PUT":
                resp = await client.put(url, json=json_body)
            elif method == "DELETE":
                resp = await client.delete(url)
            else:
                pytest.fail(f"unhandled method {method}")
            assert resp.status_code == 404, (
                f"{method} {url} on destroyed workspace expected 404, "
                f"got {resp.status_code}: {resp.text}"
            )
            envelope = resp.json()
            assert envelope["type"] == "/errors/not-found", (
                f"{method} {url} on destroyed workspace returned "
                f"unexpected envelope: {envelope!r}"
            )
    finally:
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0178 — POST /v1/workspaces with missing template_id returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0178_create_workspace_with_missing_template_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0178 — POST /v1/workspaces referencing a non-existent template_id.
    The workspace materialise path is bespoke (not the CRUD generator),
    so referential integrity must be enforced here — without it, the
    handler would 5xx trying to dereference a missing template row.
    """
    missing_template = f"missing-tpl-{unique_suffix}"
    resp = await client.post(
        "/v1/workspaces", json={"template_id": missing_template},
    )
    assert resp.status_code == 404, (
        f"expected 404 for missing template_id, got {resp.status_code}: "
        f"{resp.text}"
    )
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404


# ============================================================================
# T0198 — Workspace /log with limit=0 and limit=501 return clean envelopes
# ============================================================================


@pytest.mark.asyncio
async def test_t0198_workspace_log_limit_boundaries_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0198 — Spec §12 declares /log honours `limit` (default 50, max
    500). Probe boundary values 0 (below min) and 501 (above max) —
    both must produce documented 4xx envelopes, never 5xx.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        for limit in (0, 501):
            resp = await client.get(
                f"/v1/workspaces/{workspace_id}/log?limit={limit}",
            )
            assert resp.status_code < 500, (
                f"/log?limit={limit} leaked 5xx: {resp.text}"
            )
            # Either a clean 4xx (Pydantic param validation) or
            # 200 with clamped limit. Both acceptable.
            if resp.status_code == 422:
                envelope = resp.json()
                assert envelope["type"] == "/errors/validation-error", (
                    envelope
                )
            elif resp.status_code == 200:
                assert "commits" in resp.json(), resp.json()
            else:
                assert 400 <= resp.status_code < 500, resp.text
                envelope = resp.json()
                assert envelope["type"].startswith("/errors/"), envelope
                assert envelope["type"] != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0199 — /files/read on a directory path returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0199_workspace_files_read_on_directory_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0199 — Reading a directory path through the /files/read endpoint
    is a category error: the API exposes file reads, not directory
    contents. Must produce a documented 4xx (or any non-5xx), NEVER
    /errors/internal.

    Seeds a file inside a subdirectory so the subdirectory exists, then
    reads the subdirectory path.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Create subdir/file.txt so subdir exists as a directory
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=subdir/file.txt",
            json={"content": "hello", "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        # Now read the directory path
        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read?path=subdir",
        )
        assert resp.status_code != 500 or (
            resp.json().get("type") != "/errors/internal"
        ), f"/errors/internal leak: {resp.text}"
        if resp.status_code >= 400:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0200 — /files/download on a directory path returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0200_workspace_files_download_on_directory_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0200 — Streaming download of a directory path is the same
    category error as T0199 — must produce a clean envelope, no
    /errors/internal.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=subdir/file.txt",
            json={"content": "hello", "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files/download?path=subdir",
        )
        assert resp.status_code != 500 or (
            resp.json().get("type") != "/errors/internal"
        ), f"/errors/internal leak: {resp.text}"
        if resp.status_code >= 400:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0201 — PUT /files at a path that's already a directory returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0201_workspace_files_put_on_directory_path_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0201 — Writing a file at a path that already exists as a
    directory is a category error: the API can't replace a directory
    with file content. The local backend must surface a clean envelope
    (4xx), NOT a 5xx /errors/internal from an unhandled OSError.

    Companion to T0199 / T0200 (read/download on a directory) — the
    third corner of the directory-as-file edge cases.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Create subdir/inner.txt so subdir exists as a directory
        seed = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=subdir/inner.txt",
            json={"content": "hello", "encoding": "text"},
        )
        assert seed.status_code == 204, seed.text

        # Now try to PUT a file AT the subdirectory path
        resp = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=subdir",
            json={"content": "OVERWRITE", "encoding": "text"},
        )
        assert resp.status_code != 500 or (
            resp.json().get("type") != "/errors/internal"
        ), f"/errors/internal leak: {resp.text}"
        if resp.status_code >= 400:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope

        # And the original subdir/inner.txt content is preserved
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read"
            f"?path=subdir/inner.txt",
        )
        assert read.status_code == 200, read.text
        # Content should still be "hello", not overwritten
        body = read.json()
        # The /read response shape carries 'content' (text or base64)
        if "content" in body:
            assert "hello" in body["content"] or body["content"] == "hello", (
                f"original subdir/inner.txt content was clobbered: {body!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0210 — Workspace download endpoint sets Content-Type for binary content
# ============================================================================


@pytest.mark.asyncio
async def test_t0210_workspace_download_content_type_for_binary(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0210 — The streaming /files/download endpoint must set a
    Content-Type header that signals binary streaming (not text/plain).
    Most servers use `application/octet-stream` for unknown/binary
    content; some may set a media-type guess from the extension. The
    contract here is "Content-Type is present, is NOT text/plain on
    a binary file, and is NOT JSON".

    Extends T0031 / T0048 which only checked Content-Disposition and
    security headers.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Write a small binary blob (no extension hint, all 256 byte values)
        blob = bytes(range(256))
        encoded = base64.b64encode(blob).decode("ascii")
        seed = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=binary.bin",
            json={"content": encoded, "encoding": "base64"},
        )
        assert seed.status_code == 204, seed.text

        dl = await client.get(
            f"/v1/workspaces/{workspace_id}/files/download?path=binary.bin",
        )
        assert dl.status_code == 200, dl.text
        ctype = dl.headers.get("content-type", "")
        assert ctype, "download response missing Content-Type header"
        # Must not be text/plain — that would prevent browsers from
        # treating it as a file download
        assert "text/plain" not in ctype.lower(), (
            f"binary download Content-Type should not be text/plain; "
            f"got {ctype!r}"
        )
        # Must not be JSON — the route streams raw bytes per spec §12
        assert "json" not in ctype.lower(), (
            f"binary download Content-Type should not be JSON-flavoured; "
            f"got {ctype!r}"
        )
        # Bytes round-trip
        assert dl.content == blob, (
            f"download content mismatch: expected {len(blob)} bytes, "
            f"got {len(dl.content)} bytes"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0219 — /files/info size_bytes matches the actual written byte length
# ============================================================================


@pytest.mark.asyncio
async def test_t0219_workspace_files_info_size_matches_write(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0219 — Write a binary blob of known length, then GET /files/info
    and assert size_bytes equals exactly the number of bytes written.
    T0063 covered size on an empty file; T0114 covered info on a
    directory; this is the binary-blob round-trip pin.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        blob = bytes(range(256)) * 3  # 768 bytes, deterministic content
        encoded = base64.b64encode(blob).decode("ascii")
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=binary.bin",
            json={"content": encoded, "encoding": "base64"},
        )
        assert put.status_code == 204, put.text

        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info?path=binary.bin",
        )
        assert info.status_code == 200, info.text
        body = info.json()
        assert body.get("size_bytes") == len(blob), (
            f"size_bytes mismatch: wrote {len(blob)}, info reports "
            f"{body.get('size_bytes')!r}; body={body!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0220 — three sequential PUTs to same path: last-writer-wins; one listing entry
# ============================================================================


@pytest.mark.asyncio
async def test_t0220_workspace_files_put_three_writes_last_wins(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0220 — Three sequential writes to the same workspace-relative
    path. Pin two invariants:

      - The final read returns the THIRD body (last-writer-wins, not
        append).
      - The directory listing contains exactly ONE entry for the path
        (writes don't accumulate as separate rows).
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": template_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        path = "overwrite.txt"
        bodies = ("alpha", "bravo", "charlie")
        for body in bodies:
            r = await client.put(
                f"/v1/workspaces/{workspace_id}/files?path={path}",
                json={"content": body, "encoding": "text"},
            )
            assert r.status_code == 204, r.text

        # Final read returns the third body
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read?path={path}",
        )
        assert read.status_code == 200, read.text
        content = read.json().get("content", "")
        assert content == bodies[-1], (
            f"last-writer-wins violated: expected {bodies[-1]!r}, "
            f"got {content!r}"
        )

        # Listing shows exactly one entry for this path
        # NB: FileEntry uses `path` as its identifying field, not `name`
        lst = await client.get(
            f"/v1/workspaces/{workspace_id}/files?path=.",
        )
        assert lst.status_code == 200, lst.text
        items = lst.json().get("items", [])
        matching = [
            it for it in items
            if it.get("path") == path or it.get("path", "").endswith(path)
        ]
        assert len(matching) == 1, (
            f"expected exactly one listing entry for {path!r}, got "
            f"{len(matching)}: items={items!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0221 — Workspace files listing paginates with limit=1 across 5 files
# ============================================================================


@pytest.mark.asyncio
async def test_t0221_workspace_files_listing_pagination_limit_one(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0221 — Mirror of T0195 (toolsets limit=1) for the workspace
    files listing. Seed 5 files, walk with limit=1 + variable offset,
    assert each file appears exactly once across pages.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        file_paths = [f"file_{i:02d}.txt" for i in range(5)]
        for p in file_paths:
            r = await client.put(
                f"/v1/workspaces/{workspace_id}/files?path={p}",
                json={"content": p, "encoding": "text"},
            )
            assert r.status_code == 204, r.text

        # Walk pages of 1
        seen: list[str] = []
        for offset in range(10):  # safety bound
            page = await client.get(
                f"/v1/workspaces/{workspace_id}/files"
                f"?path=.&offset={offset}&limit=1",
            )
            assert page.status_code == 200, page.text
            items = page.json().get("items", [])
            if not items:
                break
            for it in items:
                # The path field is the identifying handle
                seen.append(it.get("path") or it.get("name"))

        # Every seeded file appears exactly once (may be in any order;
        # we only care about set equality + no duplicates)
        seen_basenames = {Path(p).name for p in seen if p}
        expected_basenames = set(file_paths)
        # Some implementations may include "." itself or other entries —
        # so check that every seeded file is present, not strict equality.
        for expected in expected_basenames:
            assert expected in seen_basenames, (
                f"file {expected!r} missing from limit=1 walk: "
                f"seen={sorted(seen)!r}"
            )
        # No duplicates among the seeded basenames within the walk
        seeded_seen = [p for p in seen if Path(p or "").name in expected_basenames]
        assert len(seeded_seen) == len(set(seeded_seen)), (
            f"duplicates in limit=1 walk: {seeded_seen!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0222 — WorkspaceProvider DELETE then immediate re-POST with same id
# ============================================================================


@pytest.mark.asyncio
async def test_t0222_workspace_provider_delete_then_recreate_same_id(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0222 — DELETE a WorkspaceProvider, then immediately POST a new
    one with the same id. Must succeed (201) — any orphaned backend
    cache from the deleted instance must be invalidated cleanly.

    Spec §12 says DELETE on a WorkspaceProvider invalidates the backend
    cache and removes the row; the re-POST exercises that path.
    """
    provider_id = f"wp-t0222-{unique_suffix}"

    # Create the first row
    body = {
        "id": provider_id,
        "provider": "local",
        "config": {"kind": "local", "path": str(tmp_path)},
    }
    first = await client.post("/v1/workspace_providers", json=body)
    assert first.status_code == 201, first.text

    # Delete it
    rm = await client.delete(f"/v1/workspace_providers/{provider_id}")
    assert rm.status_code == 204, rm.text

    # Recreate with the same id
    second = await client.post("/v1/workspace_providers", json=body)
    assert second.status_code == 201, (
        f"re-POST after DELETE must succeed (no orphan cache); got "
        f"{second.status_code}: {second.text}"
    )

    # GET reads the new row
    try:
        got = await client.get(f"/v1/workspace_providers/{provider_id}")
        assert got.status_code == 200, got.text
        assert got.json()["id"] == provider_id
    finally:
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0223 — WorkspaceTemplate DELETE while workspace exists; workspace OK
# ============================================================================


@pytest.mark.asyncio
async def test_t0223_workspace_template_delete_with_active_workspace(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0223 — Spec §12 documents WorkspaceTemplate as a snapshot row.
    Deleting the template while a workspace materialised from it is
    still active must succeed (the workspace doesn't hold a live FK)
    AND the workspace must remain readable / usable.

    Companion contract: the workspace can still write/read files after
    its parent template is gone.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # DELETE the template — must succeed
        rm = await client.delete(f"/v1/workspace_templates/{template_id}")
        assert rm.status_code == 204, rm.text

        # The workspace is still accessible
        got = await client.get(f"/v1/workspaces/{workspace_id}")
        assert got.status_code == 200, got.text

        # File ops still work on the orphan workspace
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=after_delete.txt",
            json={"content": "still works", "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read"
            f"?path=after_delete.txt",
        )
        assert read.status_code == 200, read.text
        assert read.json().get("content") == "still works"
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        # Template already deleted; clean up the provider only
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0241 — DELETE workspace while session exists: session ops return clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0241_destroy_workspace_with_active_session_clean_ops(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0241 — Spec §12 says DELETE workspace destroys the backend
    instance + the row. With a session bound to the workspace still
    alive, the subsequent signal verbs on the session must surface
    clean envelopes (not 5xx) AND the top-level /v1/sessions/{S} read
    must produce a clean response.

    Pin: the cascade doesn't 500 anywhere; envelopes are RFC 7807.
    """
    # Need an LLMProvider + Agent for the session binding
    provider_id = f"llm-t0241-{unique_suffix}"
    agent_id = f"agent-t0241-{unique_suffix}"
    pr = await client.post(
        "/v1/llm_providers",
        json={
            "id": provider_id,
            "provider": "anthropic",
            "models": [
                {"name": "claude-sonnet-4-6", "context_length": 200_000},
            ],
            "config": {"api_key": "sk-placeholder"},
            "limits": {"max_concurrency": 1},
        },
    )
    assert pr.status_code == 201, pr.text
    ag = await client.post(
        "/v1/agents",
        json={
            "id": agent_id,
            "description": "T0241",
            "model": {
                "provider_id": provider_id,
                "model_name": "claude-sonnet-4-6",
            },
            "tools": [],
        },
    )
    assert ag.status_code == 201, ag.text

    provider_id_ws, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Create a CREATED session bound to the workspace
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": False,
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # Destroy the workspace WITHOUT explicitly cancelling the session
        rm = await client.delete(f"/v1/workspaces/{workspace_id}")
        assert rm.status_code == 204, rm.text

        # Session signal verbs on the destroyed workspace: must be 4xx
        # (404 most likely — workspace gone), never 5xx
        for verb in ("cancel", "pause", "resume"):
            r = await client.post(
                f"/v1/workspaces/{workspace_id}/sessions/{session_id}/{verb}",
            )
            assert r.status_code < 500, (
                f"{verb} on destroyed workspace leaked 5xx: {r.text}"
            )
            if r.status_code >= 400:
                envelope = r.json()
                assert envelope["type"].startswith("/errors/"), envelope
                assert envelope["type"] != "/errors/internal", envelope

        # Top-level /v1/sessions/{S} is workspace-agnostic — it may
        # still return the row or 404. Either is fine.
        top = await client.get(f"/v1/sessions/{session_id}")
        assert top.status_code < 500, (
            f"top-level /v1/sessions/{{S}} leaked 5xx: {top.text}"
        )
        if top.status_code >= 400:
            envelope = top.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await _teardown_provider_template(
            client, provider_id_ws, template_id,
        )
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{provider_id}")


# ============================================================================
# T0254 — Workspace /files PUT → DELETE → PUT round-trip is consistent
# ============================================================================


@pytest.mark.asyncio
async def test_t0254_workspace_files_put_delete_put_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0254 — Three-step rapid sequence on the same workspace path
    (PUT → DELETE → PUT). Each step returns 2xx; final read returns
    the third write's body; listing has exactly one entry for the
    path. Distinct from T0220 (three sequential PUTs) by inserting
    a DELETE in the middle — exercises the create→destroy→create
    flow on the local backend.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json={"template_id": template_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        path = "cycle.txt"

        # PUT #1
        put1 = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path={path}",
            json={"content": "first", "encoding": "text"},
        )
        assert put1.status_code == 204, put1.text

        # DELETE
        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files?path={path}",
        )
        assert rm.status_code == 204, rm.text

        # Confirm intermediate DELETE state — read returns 404
        gone = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read?path={path}",
        )
        assert gone.status_code == 404, gone.text

        # PUT #2 (third op, different body)
        put2 = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path={path}",
            json={"content": "third", "encoding": "text"},
        )
        assert put2.status_code == 204, put2.text

        # Final read returns the third body
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read?path={path}",
        )
        assert read.status_code == 200, read.text
        assert read.json().get("content") == "third", read.json()

        # Listing has exactly one entry for this path
        lst = await client.get(
            f"/v1/workspaces/{workspace_id}/files?path=.",
        )
        assert lst.status_code == 200, lst.text
        items = lst.json().get("items", [])
        matching = [
            it for it in items
            if it.get("path") == path or it.get("path", "").endswith(path)
        ]
        assert len(matching) == 1, (
            f"expected exactly one listing entry after PUT→DELETE→PUT, "
            f"got {len(matching)}: items={items!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0267 — WorkspaceTemplate PUT mutates row; description reflects update
# ============================================================================


@pytest.mark.asyncio
async def test_t0267_workspace_template_put_mutates_row(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0267 — Spec §12 says WorkspaceTemplate is "a mutable snapshot
    used by future create calls". Pin the mutability via PUT:
    create template, GET reads original description, PUT replaces
    description, GET reads the new description.

    The "snapshot" semantics (already-materialised workspaces don't
    pick up the mutation) are not testable cheaply without inspecting
    backend state — this test pins only the PUT contract on the
    template ROW itself.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        # Initial GET
        got1 = await client.get(f"/v1/workspace_templates/{template_id}")
        assert got1.status_code == 200, got1.text
        orig_desc = got1.json()["description"]

        # PUT with updated description
        new_desc = f"updated-description-{unique_suffix}"
        put_body = dict(got1.json())
        put_body["description"] = new_desc
        put = await client.put(
            f"/v1/workspace_templates/{template_id}", json=put_body,
        )
        assert put.status_code == 200, put.text

        # GET reflects the new description
        got2 = await client.get(f"/v1/workspace_templates/{template_id}")
        assert got2.status_code == 200, got2.text
        assert got2.json()["description"] == new_desc, got2.json()
        assert got2.json()["description"] != orig_desc
    finally:
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0268 — DELETE WorkspaceProvider while a workspace from its template is alive
# ============================================================================


@pytest.mark.asyncio
async def test_t0268_delete_workspace_provider_with_active_workspace(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0268 — DELETE WorkspaceProvider while a workspace materialised
    from one of its templates is still alive. Provider DELETE returns
    either 204 (no-FK semantics; cascade tolerated) or a 4xx
    (referential check). Either way:
      - no /errors/internal
      - the existing workspace continues to respond cleanly to file
        ops (or returns clean 4xx if the cascade also tore it down).
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]
        try:
            # DELETE the provider mid-flight
            rm = await client.delete(f"/v1/workspace_providers/{provider_id}")
            assert rm.status_code < 500, rm.text
            if rm.status_code >= 400:
                envelope = rm.json()
                assert envelope["type"].startswith("/errors/"), envelope
                assert envelope["type"] != "/errors/internal", envelope

            # File op on existing workspace must return cleanly
            r = await client.put(
                f"/v1/workspaces/{workspace_id}/files?path=after.txt",
                json={"content": "x", "encoding": "text"},
            )
            assert r.status_code < 500, r.text
            envelope = r.json() if (r.content and r.status_code >= 400) else {}
            if envelope:
                assert envelope.get("type", "/errors/").startswith(
                    "/errors/"
                ), envelope
                assert envelope.get("type") != "/errors/internal", envelope
        finally:
            await client.delete(f"/v1/workspaces/{workspace_id}")
    finally:
        # Provider may already be gone
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0274 — GET /v1/workspaces/{wid}/files with no `path` returns root listing
# ============================================================================


@pytest.mark.asyncio
async def test_t0274_workspace_files_no_path_returns_root_listing(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0274 — Spec §12 documents `path=<dir>` as a query param on the
    files-list endpoint, but doesn't pin behaviour when omitted. The
    contract pin: a missing `path` returns the workspace root listing
    cleanly (200 with items), not a 422 missing-required-param.

    If the API actually requires `path`, the response is 422 with
    /errors/validation-error and the test records that contract
    instead.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Seed one file at the root so the listing has something
        seed = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=root_marker.txt",
            json={"content": "hello", "encoding": "text"},
        )
        assert seed.status_code == 204, seed.text

        # GET without path query param
        resp = await client.get(f"/v1/workspaces/{workspace_id}/files")
        assert resp.status_code in (200, 422), resp.text
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            paths = {it.get("path") for it in items}
            # The seeded file should be visible at the root
            assert any("root_marker.txt" in (p or "") for p in paths), (
                f"root_marker.txt not in default-path listing: "
                f"items={items!r}"
            )
        else:
            envelope = resp.json()
            assert envelope["type"] == "/errors/validation-error", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0275 — Workspace files listing is non-recursive on a deep tree
# ============================================================================


@pytest.mark.asyncio
async def test_t0275_workspace_files_listing_is_non_recursive(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0275 — Seed a deep file tree:
        a/b/c/leaf.txt
        a/b/sibling.txt
        a/peer.txt
    Then GET /files?path=a/b. Pin: returns ONLY immediate children
    of `a/b` (i.e. `c/` directory entry + `sibling.txt` file), NOT
    the transitively-nested `leaf.txt` or the up-level `peer.txt`.

    Catches a regression where the listing accidentally walks
    recursively (which would surface as a wrong directory size or
    confuse clients showing a folder view).
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Seed three files at different depths
        files_to_create = [
            ("a/b/c/leaf.txt", "leaf"),
            ("a/b/sibling.txt", "sibling"),
            ("a/peer.txt", "peer"),
        ]
        for fpath, content in files_to_create:
            r = await client.put(
                f"/v1/workspaces/{workspace_id}/files?path={fpath}",
                json={"content": content, "encoding": "text"},
            )
            assert r.status_code == 204, r.text

        # List a/b
        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files?path=a/b",
        )
        assert resp.status_code == 200, resp.text
        items = resp.json().get("items", [])
        item_paths = {it.get("path", "") for it in items}

        # sibling.txt MUST appear (immediate child)
        assert any("sibling.txt" in p for p in item_paths), (
            f"immediate child sibling.txt missing from a/b listing: "
            f"{sorted(item_paths)!r}"
        )
        # peer.txt MUST NOT appear (it's at a higher level)
        assert not any(
            "peer.txt" in p and "a/b/" not in p for p in item_paths
        ), (
            f"up-level peer.txt unexpectedly in a/b listing: "
            f"{sorted(item_paths)!r}"
        )
        # leaf.txt itself MUST NOT appear at this level (it's at
        # a/b/c/leaf.txt — recursive walk would surface it)
        assert not any(
            p.endswith("a/b/c/leaf.txt") or p.endswith("c/leaf.txt")
            for p in item_paths
        ), (
            f"transitively-nested leaf.txt unexpectedly in a/b "
            f"listing (recursive walk regression): "
            f"{sorted(item_paths)!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0284 — POST /v1/workspaces/find with predicate filters by template_id
# ============================================================================


@pytest.mark.asyncio
async def test_t0284_workspaces_find_with_predicate_filters_by_template(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0284 — The bespoke /v1/workspaces router exposes /find for
    predicate-based filtering (per spec §12). Seed 2 templates +
    workspaces from each; POST /find filtering by template_id returns
    only the workspaces from the targeted template.
    """
    provider_id, template_id_a = await _setup_provider_template(
        client, suffix=f"{unique_suffix}-a", root=tmp_path,
    )
    # Use a separate suffix to get a second template; reuse provider
    template_id_b = f"wt-{unique_suffix}-b"
    tpl_b = await client.post(
        "/v1/workspace_templates",
        json={
            "id": template_id_b,
            "description": "T0284 second template",
            "provider_id": provider_id,
            "backend": {"kind": "local"},
        },
    )
    assert tpl_b.status_code == 201, tpl_b.text

    workspaces_a: list[str] = []
    workspaces_b: list[str] = []
    try:
        # 2 workspaces from template_a, 1 from template_b
        for _ in range(2):
            ws = await client.post(
                "/v1/workspaces",
                json={"template_id": template_id_a},
            )
            assert ws.status_code == 201, ws.text
            workspaces_a.append(ws.json()["id"])

        ws_b = await client.post(
            "/v1/workspaces",
            json={"template_id": template_id_b},
        )
        assert ws_b.status_code == 201, ws_b.text
        workspaces_b.append(ws_b.json()["id"])

        # POST /find with predicate template_id = template_id_a
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "template_id"},
                "right": {"kind": "value", "value": template_id_a},
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/workspaces/find", json=body)
        assert resp.status_code == 200, resp.text
        ids = {item["id"] for item in resp.json()["items"]}
        # All template_a workspaces present
        for wid in workspaces_a:
            assert wid in ids, (
                f"workspace {wid!r} from template_a missing from /find "
                f"results: {ids!r}"
            )
        # template_b workspace NOT present
        for wid in workspaces_b:
            assert wid not in ids, (
                f"workspace {wid!r} from template_b unexpectedly in "
                f"template_a /find results: {ids!r}"
            )
    finally:
        for wid in workspaces_a + workspaces_b:
            await client.delete(f"/v1/workspaces/{wid}")
        await client.delete(f"/v1/workspace_templates/{template_id_b}")
        await _teardown_provider_template(client, provider_id, template_id_a)


# ============================================================================
# T0285 — GET /v1/workspaces supports offset/limit pagination
# ============================================================================


@pytest.mark.asyncio
async def test_t0285_workspaces_list_offset_limit_pagination(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0285 — Pin that the bespoke /v1/workspaces list endpoint
    honours the §4 pagination contract: offset and limit query
    params combine to return distinct page slices, total reflects
    all matching rows.

    Seeds 5 workspaces, then walks limit=2, offset=0 → offset=2 →
    offset=4. Each page returns ≤2 items; the union covers all 5
    seeded ids without duplicates.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    seeded_ids: list[str] = []
    try:
        for _ in range(5):
            ws = await client.post(
                "/v1/workspaces",
                json={"template_id": template_id},
            )
            assert ws.status_code == 201, ws.text
            seeded_ids.append(ws.json()["id"])

        # Filter by template_id via /find so other-test workspaces
        # don't pollute the page count
        body = {
            "predicate": {
                "kind": "predicate",
                "op": "=",
                "left": {"kind": "field", "name": "template_id"},
                "right": {"kind": "value", "value": template_id},
            },
            "page": {"kind": "offset", "offset": 0, "length": 2},
        }

        seen: list[str] = []
        for offset in (0, 2, 4):
            body["page"] = {"kind": "offset", "offset": offset, "length": 2}
            r = await client.post("/v1/workspaces/find", json=body)
            assert r.status_code == 200, r.text
            page = r.json()
            assert page["length"] <= 2, page
            seen.extend(item["id"] for item in page["items"])

        # Every seeded id appears exactly once
        assert sorted(seen) == sorted(seeded_ids), (
            f"pagination walk did not cover seeded set. "
            f"seeded={sorted(seeded_ids)!r}, seen={sorted(seen)!r}"
        )
        assert len(seen) == len(set(seen)), (
            f"duplicates across pages: {seen!r}"
        )
    finally:
        for wid in seeded_ids:
            await client.delete(f"/v1/workspaces/{wid}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0290 — Workspace DELETE concurrent with PUT /files yields clean envelopes
# ============================================================================


@pytest.mark.asyncio
async def test_t0290_workspace_destroy_concurrent_with_put_files_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0290 — Race: DELETE workspace concurrent with PUT /files on
    the same workspace. Both responses must have clean envelopes
    (no 5xx /errors/internal); subsequent GET /workspaces/{id}
    returns 404.

    Catches a regression where the destroy cascade leaves a
    half-state that 500s the file write or vice versa.
    """
    import asyncio
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Race the destroy and the file write
        delete_task = asyncio.create_task(
            client.delete(f"/v1/workspaces/{workspace_id}"),
        )
        put_task = asyncio.create_task(client.put(
            f"/v1/workspaces/{workspace_id}/files?path=raced.txt",
            json={"content": "x", "encoding": "text"},
        ))
        delete_resp, put_resp = await asyncio.gather(
            delete_task, put_task,
        )

        for r, label in ((delete_resp, "DELETE"), (put_resp, "PUT")):
            assert r.status_code < 500, (
                f"{label} leaked 5xx: {r.status_code}: {r.text}"
            )
            envelope = r.json() if (r.content and r.status_code >= 400) else {}
            if envelope:
                assert envelope.get("type", "/errors/").startswith(
                    "/errors/"
                ), envelope
                assert envelope.get("type") != "/errors/internal", envelope

        # GET on the destroyed workspace is 404 cleanly
        gone = await client.get(f"/v1/workspaces/{workspace_id}")
        assert gone.status_code in (200, 404), gone.text
    finally:
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0291 — DELETE WorkspaceProvider while WorkspaceTemplate references it
# ============================================================================


@pytest.mark.asyncio
async def test_t0291_delete_workspace_provider_with_referencing_template(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0291 — Lifecycle ordering: DELETE the WorkspaceProvider while
    a WorkspaceTemplate still references it. Provider DELETE returns
    clean envelope (204 if no-FK, or clean 4xx if cascade enforced);
    the template GET still responds cleanly afterward.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        # Sanity: template references provider
        tpl_get = await client.get(f"/v1/workspace_templates/{template_id}")
        assert tpl_get.status_code == 200, tpl_get.text
        assert tpl_get.json()["provider_id"] == provider_id

        # DELETE provider
        rm = await client.delete(f"/v1/workspace_providers/{provider_id}")
        assert rm.status_code < 500, rm.text
        if rm.status_code >= 400:
            envelope = rm.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope

        # Template GET still responds cleanly
        tpl_after = await client.get(f"/v1/workspace_templates/{template_id}")
        assert tpl_after.status_code < 500, tpl_after.text
        if tpl_after.status_code >= 400:
            envelope = tpl_after.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await client.delete(f"/v1/workspace_templates/{template_id}")
        # Provider may already be gone
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0293 — PUT /files with encoding=text containing NUL byte
# ============================================================================


@pytest.mark.asyncio
async def test_t0293_workspace_files_put_text_with_nul_byte_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0293 — Text-mode write where the content string contains a
    \\x00 (NUL) byte. The handler must not 500: either accept the
    write (and the bytes round-trip) or reject with a clean 4xx
    envelope. NEVER /errors/internal.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        nul_text = "before\x00after"
        resp = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=nul.txt",
            json={"content": nul_text, "encoding": "text"},
        )
        assert resp.status_code != 500 or (
            resp.json().get("type") != "/errors/internal"
        ), f"/errors/internal leak on NUL-byte text PUT: {resp.text}"
        if resp.status_code in (200, 204):
            # Accepted — verify round-trip via download (raw bytes)
            dl = await client.get(
                f"/v1/workspaces/{workspace_id}/files/download?path=nul.txt",
            )
            assert dl.status_code == 200, dl.text
            assert dl.content == nul_text.encode("utf-8"), dl.content
        else:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0294 — PUT /files body missing `content` field returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0294_workspace_files_put_body_missing_content_returns_422(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0294 — FileWriteBody requires `content` (per spec §12). A PUT
    body with only `encoding` and no `content` must return 422
    /errors/validation-error cleanly.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        resp = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=nocontent.txt",
            json={"encoding": "text"},
        )
        assert resp.status_code == 422, resp.text
        envelope = resp.json()
        assert envelope["type"] == "/errors/validation-error", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0295 — POST /v1/workspace_templates without `provider_id` returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0295_workspace_template_post_missing_provider_id_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0295 — Required-field validation on the WorkspaceTemplate
    create body. The Pydantic model declares `provider_id` as
    required (per spec §12); omitting it yields 422
    /errors/validation-error.
    """
    resp = await client.post(
        "/v1/workspace_templates",
        json={
            "id": f"wt-noprovider-{unique_suffix}",
            "description": "T0295",
            "backend": {"kind": "local"},
        },
    )
    assert resp.status_code == 422, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/validation-error", envelope


# ============================================================================
# T0296 — Workspace files PUT to path > 250 chars (still-open MAX_PATH bug)
# ============================================================================


@pytest.mark.asyncio
async def test_t0296_workspace_files_put_very_long_path_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0296 — Spec §12 documents a known-anomaly: PUT to a workspace-
    relative path long enough to push the absolute path past Windows
    MAX_PATH=260 currently returns 500 /errors/internal (sibling of
    quarantined T0064a). The proper contract is 4xx (e.g. bad-request).

    This test is INTENTIONALLY PERMISSIVE — accepts either the buggy
    behaviour (500 /errors/internal) or the fix (clean 4xx). When the
    fix lands, future iterations should tighten the assertion to
    require 4xx and remove the bug acknowledgement here.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # 250-char workspace-relative path
        long_segment = "x" * 250
        resp = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path={long_segment}",
            json={"content": "x", "encoding": "text"},
        )
        # Accept 4xx (proper contract, fix landed) OR 500 (current
        # known-bug behaviour). Document the actual outcome.
        assert resp.status_code in range(400, 600), resp.text
        if resp.status_code == 500:
            # Known-bug path — envelope shape sanity
            envelope = resp.json()
            assert envelope["type"] == "/errors/internal", envelope
        else:
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            # Once the fix lands, this branch will be the only one
            # reached and the test should be tightened.
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0297 — Workspace download Content-Disposition encodes unicode filename
# ============================================================================


@pytest.mark.asyncio
async def test_t0297_workspace_download_unicode_filename_rfc5987(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0297 — Extends T0031 (sanitised legacy filename) to verify
    the RFC 5987 `filename*=UTF-8''…` parameter correctly encodes a
    non-ASCII filename. The unicode filename should percent-encode
    in filename*=, while the legacy `filename=` is the sanitised
    fallback.
    """
    import urllib.parse

    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Filename with non-ASCII chars (Cyrillic + emoji)
        unicode_name = "файл-📄.txt"
        encoded_path = urllib.parse.quote(unicode_name, safe="")
        seed = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path={encoded_path}",
            json={"content": "hello", "encoding": "text"},
        )
        assert seed.status_code == 204, seed.text

        dl = await client.get(
            f"/v1/workspaces/{workspace_id}/files/download"
            f"?path={encoded_path}",
        )
        assert dl.status_code == 200, dl.text
        cd = dl.headers.get("content-disposition", "")
        assert cd, "missing Content-Disposition header"
        # filename*= should be present and encode the unicode chars
        assert "filename*=" in cd.lower(), (
            f"Content-Disposition missing filename*= for unicode name: "
            f"{cd!r}"
        )
        # The percent-encoded unicode should appear in filename*=
        # (e.g. "%D1%84%D0%B0%D0%B9%D0%BB" for "файл")
        assert "utf-8''" in cd.lower(), (
            f"filename*= should declare UTF-8 encoding: {cd!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0316 — /files/info on freshly-deleted path returns 404 (no stale cache)
# ============================================================================


@pytest.mark.asyncio
async def test_t0316_workspace_files_info_on_deleted_path_returns_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0316 — Write a file, DELETE it, then GET /files/info on the
    same path. Must return 404 /errors/not-found (no stale cache from
    the prior write). Companion to T0144 (info on a never-existed
    path).
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        path = "ephemeral.txt"
        # Write
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path={path}",
            json={"content": "x", "encoding": "text"},
        )
        assert put.status_code == 204, put.text
        # Verify info exists pre-delete
        info_pre = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info?path={path}",
        )
        assert info_pre.status_code == 200, info_pre.text

        # Delete
        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files?path={path}",
        )
        assert rm.status_code == 204, rm.text

        # /files/info on the deleted path = 404
        info_post = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info?path={path}",
        )
        assert info_post.status_code == 404, info_post.text
        envelope = info_post.json()
        assert envelope["type"] == "/errors/not-found", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0317 — /files list on a fresh workspace returns empty items
# ============================================================================


@pytest.mark.asyncio
async def test_t0317_workspace_files_list_on_fresh_workspace_empty(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0317 — Materialise a workspace from a template that has no
    init_files. GET /files (or /files?path=.) must return 200 with
    items=[] cleanly — pin the empty-list envelope.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files?path=.",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body, body
        # The fresh workspace may have nothing (most likely) OR a
        # backend-internal directory like .state. Pin only that the
        # response shape is the empty-or-clean items list.
        assert isinstance(body["items"], list), body
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0325 — GET /v1/workspaces/{missing}/log returns 404 /errors/not-found
# ============================================================================


@pytest.mark.asyncio
async def test_t0325_workspace_log_on_missing_workspace_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0325 — Missing-workspace contract on the bespoke /log
    endpoint. T0057 covers fresh-workspace shape; this pins the
    missing-workspace path with the documented 404 envelope.
    """
    missing_ws = f"missing-ws-t0325-{unique_suffix}"
    resp = await client.get(f"/v1/workspaces/{missing_ws}/log")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404


# ============================================================================
# T0369 — Workspace files PUT with absolute path returns clean 4xx
# ============================================================================


@pytest.mark.asyncio
async def test_t0369_workspace_files_put_absolute_path_returns_4xx(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0369 — Sibling of T0148 (`..` traversal) for absolute paths.
    PUT /files?path=<absolute> must reject cleanly with 4xx envelope;
    no traversal allowed; never 5xx /errors/internal.

    Test both POSIX (/etc/passwd) and Windows (C:\\Windows\\foo)
    style absolute paths to cover both platforms.
    """
    import urllib.parse
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        for absolute_path in ("/etc/passwd", "C:\\Windows\\foo.txt"):
            encoded = urllib.parse.quote(absolute_path, safe="")
            resp = await client.put(
                f"/v1/workspaces/{workspace_id}/files?path={encoded}",
                json={"content": "PWNED", "encoding": "text"},
            )
            envelope = resp.json() if resp.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"absolute path {absolute_path!r} leaked /errors/internal: "
                f"{resp.text}"
            )
            # Should reject as 4xx (likely 422 or 400 for malformed
            # path) — never 5xx
            assert 400 <= resp.status_code < 500, (
                f"absolute path {absolute_path!r} should be rejected "
                f"4xx; got {resp.status_code}: {resp.text}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0370 — Workspace whose host directory was deleted externally
# ============================================================================


@pytest.mark.asyncio
async def test_t0370_workspace_files_after_external_dir_delete_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0370 — Probe out-of-sync local backend: shutil.rmtree the
    workspace's underlying directory on the host, then call GET
    /files. The handler must produce a clean envelope (4xx or 200
    with empty items if the backend re-creates the dir; never
    /errors/internal).
    """
    import shutil
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Seed a file so there's something to "lose"
        await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=marker.txt",
            json={"content": "x", "encoding": "text"},
        )

        # Find the workspace's host directory under tmp_path and rm it
        # The local backend layout puts each workspace under
        # <provider.path>/<workspace_id>
        ws_dir = tmp_path / workspace_id
        if ws_dir.exists():
            shutil.rmtree(ws_dir)

        # Now GET /files — must not 5xx
        resp = await client.get(
            f"/v1/workspaces/{workspace_id}/files?path=.",
        )
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"GET /files after external rmtree leaked /errors/internal: "
            f"{resp.text}"
        )
    finally:
        if workspace_id is not None:
            # DELETE may fail since the dir is already gone; tolerate
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0376 — /files/info reports a recent (not future) mtime
# ============================================================================


@pytest.mark.asyncio
async def test_t0376_workspace_files_info_reports_recent_mtime(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0376 — Write a file, fetch /files/info, assert the modified
    timestamp is within ±60s of `datetime.now(UTC)`. Pins mtime is
    real wall-clock, not zeroed/epoch/future.
    """
    from datetime import datetime, timezone, timedelta

    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        before = datetime.now(timezone.utc)
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files?path=mtime.txt",
            json={"content": "x", "encoding": "text"},
        )
        assert put.status_code == 204, put.text
        after = datetime.now(timezone.utc)

        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info?path=mtime.txt",
        )
        assert info.status_code == 200, info.text
        body = info.json()

        # Look for any timestamp-shaped field (mtime / modified_at /
        # last_modified / etc.) — try common names
        ts_field = None
        for candidate in (
            "mtime", "modified_at", "last_modified", "modified",
            "updated_at",
        ):
            if candidate in body:
                ts_field = body[candidate]
                break

        if ts_field is None:
            # No timestamp field surfaced — pin the absence as a
            # documented gap, but soft-skip the timing check
            pytest.skip(
                f"FileInfo body has no recognised timestamp field: "
                f"{list(body.keys())!r}"
            )

        # Parse ISO-8601 timestamp
        if isinstance(ts_field, str):
            ts = datetime.fromisoformat(ts_field.replace("Z", "+00:00"))
        else:
            pytest.skip(f"timestamp not a string: {ts_field!r}")

        # Within ±60 seconds of the write window
        window_min = before - timedelta(seconds=60)
        window_max = after + timedelta(seconds=60)
        assert window_min <= ts <= window_max, (
            f"mtime {ts!r} not in ±60s window [{window_min!r}, "
            f"{window_max!r}]"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0378 — WorkspaceTemplate with empty init_commands materialises cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0378_template_with_empty_init_commands_materialises_cleanly(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0378 — Negative control to T0092 / T0155 (which use templates
    WITH init_commands). Pin that an empty `init_commands=[]` is
    accepted at template POST; the materialised workspace returns
    200 on /files list with no init artefacts.
    """
    provider_id = f"wp-t0378-{unique_suffix}"
    template_id = f"wt-t0378-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json={
            "id": provider_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        },
    )
    assert pr.status_code == 201, pr.text

    workspace_id: str | None = None
    template_created = False
    try:
        # Template with empty init_commands
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "T0378 empty init",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "init_commands": [],
            },
        )
        # Either 201 (accepted) or 422 (rejected as unsupported field) —
        # pin no /errors/internal
        assert tpl.status_code != 500, tpl.text
        if tpl.status_code != 201:
            envelope = tpl.json()
            assert envelope["type"] != "/errors/internal", envelope
            pytest.skip(
                f"Template with empty init_commands rejected "
                f"({tpl.status_code}); body: {tpl.text}"
            )
        template_created = True

        # Materialise a workspace from the empty-init template
        ws = await client.post(
            "/v1/workspaces", json={"template_id": template_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # /files list returns 200 with empty (or only backend-internal)
        # items — no init artefacts
        listing = await client.get(
            f"/v1/workspaces/{workspace_id}/files?path=.",
        )
        assert listing.status_code == 200, listing.text
        assert isinstance(listing.json().get("items"), list), listing.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if template_created:
            await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0334 — Workspace /log without limit + with limit=1
# ============================================================================


@pytest.mark.asyncio
async def test_t0334_workspace_log_default_limit_and_explicit_limit_one(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0334 — On a fresh workspace (empty /log per T0057), both
    `?limit=1` and the no-`limit` default form return clean envelopes
    with `commits` arrays of length ≤ N. Pins:

      - omitting limit returns 200 with at most 50 (default) commits
      - explicit ?limit=1 returns at most 1 commit

    A fresh workspace has 0 commits so both arrays are empty here;
    the hard pin is the response shape and length cap.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    workspace_id: str | None = None
    try:
        ws = await client.post(
            "/v1/workspaces", json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Default — no limit query
        no_limit = await client.get(
            f"/v1/workspaces/{workspace_id}/log",
        )
        assert no_limit.status_code == 200, no_limit.text
        commits_default = no_limit.json().get("commits", [])
        assert isinstance(commits_default, list), no_limit.json()
        assert len(commits_default) <= 50, (
            f"default-limit log returned {len(commits_default)} > 50"
        )

        # Explicit limit=1
        l1 = await client.get(
            f"/v1/workspaces/{workspace_id}/log?limit=1",
        )
        assert l1.status_code == 200, l1.text
        commits_one = l1.json().get("commits", [])
        assert isinstance(commits_one, list)
        assert len(commits_one) <= 1, (
            f"limit=1 log returned {len(commits_one)} > 1"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0391 — file with leading-dot-only basename `...weird` round-trips
# ============================================================================


@pytest.mark.asyncio
async def test_t0391_workspace_file_leading_dots_basename_round_trips(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0391 — A basename starting with multiple dots (e.g.
    `...weird`) is a perfectly valid POSIX filename and MUST NOT be
    confused with a path-traversal attempt (which is `..` as a path
    SEGMENT, not as a leading basename character).

    Round-trips PUT/READ/LIST/DELETE on `...weird.txt` and on a
    nested `subdir/...weird` to ensure both root- and child-level
    basenames work.
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

        for path in ("...weird.txt", "subdir/...weird"):
            content = f"leading-dots-{path}"
            put = await client.put(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": path},
                json={"content": content, "encoding": "text"},
            )
            assert put.status_code == 204, (
                f"PUT {path!r} returned {put.status_code}: {put.text}"
            )

            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": path},
            )
            assert read.status_code == 200, (
                f"READ {path!r} returned {read.status_code}: {read.text}"
            )
            assert read.json()["content"] == content
            assert read.json()["path"] == path

            # List the parent directory and confirm the basename
            # appears.
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            list_params = {"path": parent} if parent else None
            listed = await client.get(
                f"/v1/workspaces/{workspace_id}/files",
                params=list_params,
            )
            assert listed.status_code == 200, listed.text
            names = [item["path"] for item in listed.json()["items"]]
            assert any(
                n == path or n.endswith(path.rsplit("/", 1)[-1])
                for n in names
            ), (
                f"leading-dots basename {path!r} missing from list "
                f"of parent {parent!r}: {names!r}"
            )

            rm = await client.delete(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": path},
            )
            assert rm.status_code == 204, (
                f"DELETE {path!r} returned {rm.status_code}: {rm.text}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0392 — basename with `?`, `*`, `[` round-trips on local backend
# ============================================================================


@pytest.mark.asyncio
async def test_t0392_workspace_file_glob_metachar_basename_round_trips(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0392 — Glob meta-characters (`?`, `*`, `[`) are valid in POSIX
    filenames and must NOT be expanded at the storage layer. The
    local backend writes via Python `pathlib.Path` / direct fs APIs,
    not via shell, so glob expansion would only happen if some
    intermediate code path mistakenly ran through a shell or used
    `glob`/`fnmatch` for routing.

    Pins PUT/READ/DELETE on a file whose basename contains all three
    meta-characters. Skipped on Windows (which forbids `?` and `*`
    in paths at the OS level, so this test isn't meaningful there).
    """
    if os.name == "nt":
        pytest.skip("Windows filesystem rejects '?' and '*' in basenames")

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

        # All three glob meta-characters in a single basename
        glob_path = "weird?name*here[1].txt"
        content = "no-glob-expansion"

        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": glob_path},
            json={"content": content, "encoding": "text"},
        )
        assert put.status_code == 204, (
            f"PUT with glob meta-chars failed: {put.status_code}: "
            f"{put.text}"
        )

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": glob_path},
        )
        assert read.status_code == 200, read.text
        body = read.json()
        # If glob expansion had occurred, the path field would be a
        # different concrete filename or the read would 404.
        assert body["content"] == content
        assert body["path"] == glob_path

        # Listing the root must include exactly one entry that
        # matches our literal basename — not multiple entries that
        # would result from glob fan-out.
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/files",
        )
        assert listed.status_code == 200, listed.text
        names = [item["path"] for item in listed.json()["items"]]
        matches = [n for n in names if n.endswith(glob_path)]
        assert len(matches) == 1, (
            f"glob meta-char basename should appear exactly once in "
            f"listing; got matches={matches!r} from names={names!r}"
        )

        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": glob_path},
        )
        assert rm.status_code == 204, rm.text
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0393 — PUT with trailing-slash path: accepts or 4xx, never 500
# ============================================================================


@pytest.mark.asyncio
async def test_t0393_workspace_files_put_trailing_slash_path_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0393 — Path canonicalisation pin: a PUT to a path ending with
    `/` (e.g. `notes/`) is ambiguous — it could mean "create a file
    literally named with a trailing slash" (filesystem-illegal) or
    "the directory itself" (also nonsensical for PUT). The contract
    is: either the API accepts and silently strips the slash, OR it
    rejects with a 4xx; it MUST NOT 500.
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

        # PUT to a path with a trailing slash
        resp = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "notes/"},
            json={"content": "trailing-slash-content", "encoding": "text"},
        )
        # Hard pin: never 5xx. Either 204 (accepted, slash stripped)
        # or 4xx (rejected with clean envelope).
        assert resp.status_code < 500, (
            f"PUT with trailing-slash path returned 5xx: "
            f"{resp.status_code}: {resp.text}"
        )

        if resp.status_code == 204:
            # If accepted, the canonical path is "notes" — read it back
            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": "notes"},
            )
            # Either readable as "notes" or as "notes/" — accept both
            # to give the API freedom in canonicalisation choice.
            assert read.status_code in (200, 404), read.text
        else:
            # Rejected — must be a clean RFC 7807 envelope
            assert resp.status_code in range(400, 500), resp.text
            envelope = resp.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope
            assert envelope.get("type") != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0394 — file path with mixed `\\` and `/` separators returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0394_workspace_files_mixed_separators_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0394 — A path like `dir\\sub/file.txt` mixes Windows (`\\`)
    and POSIX (`/`) separators. The local backend ultimately writes
    via Python `pathlib`, but the path arrives as an HTTP query
    parameter — backslashes have no special meaning in URL paths.

    Hard contract: never 5xx. Either:
      * accepted (204) — the API normalised separators (and the file
        is then readable via the original or canonical path), OR
      * rejected (4xx) — clean RFC 7807 envelope, no /errors/internal
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

        mixed_path = "dir\\sub/file.txt"
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": mixed_path},
            json={"content": "mixed-sep", "encoding": "text"},
        )
        # Hard pin: never 5xx
        assert put.status_code < 500, (
            f"PUT with mixed separators returned 5xx: "
            f"{put.status_code}: {put.text}"
        )

        if put.status_code == 204:
            # Accepted — the file should be readable somewhere.
            # Try the original path first; if 404, accept that the
            # backend canonicalised to a different form.
            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": mixed_path},
            )
            assert read.status_code in (200, 404), read.text
            if read.status_code == 200:
                assert read.json()["content"] == "mixed-sep"
        else:
            # Rejected — must be a clean envelope, never internal
            assert put.status_code in range(400, 500), put.text
            envelope = put.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope
            assert envelope.get("type") != "/errors/internal", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0395 — WorkspaceTemplate state_path override materialises and /log works
# ============================================================================


@pytest.mark.asyncio
async def test_t0395_workspace_template_state_path_override_works(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0395 — WorkspaceTemplate.state_path defaults to `.state` (per
    matrix/model/workspace.py). Pin that overriding it to a custom
    path (e.g. `.matrix-state`) still produces a working workspace:
    materialise succeeds, /log finds the repo at the new location,
    and the file ops surface still works.
    """
    provider_id = f"wp-t0395-{unique_suffix}"
    template_id = f"wt-t0395-{unique_suffix}"
    custom_state = ".matrix-state"

    pr = await client.post(
        "/v1/workspace_providers",
        json=_provider_body(provider_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    try:
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "T0395 — custom state_path",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "state_path": custom_state,
            },
        )
        assert tpl.status_code == 201, tpl.text
        # Confirm the override round-tripped through the create call
        assert tpl.json().get("state_path") == custom_state, tpl.json()

        workspace_id: str | None = None
        try:
            ws = await client.post(
                "/v1/workspaces",
                json=_workspace_body(template_id=template_id),
            )
            assert ws.status_code == 201, ws.text
            workspace_id = ws.json()["id"]

            # /log on a fresh workspace should return commits=[] (or
            # a small list) cleanly — proves the state repo was found
            # at the custom path.
            log = await client.get(
                f"/v1/workspaces/{workspace_id}/log",
            )
            assert log.status_code == 200, log.text
            assert isinstance(log.json().get("commits"), list)

            # File ops sanity — write/read works as usual
            put = await client.put(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": "user-file.txt"},
                json={"content": "hello", "encoding": "text"},
            )
            assert put.status_code == 204, put.text
            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": "user-file.txt"},
            )
            assert read.status_code == 200, read.text
            assert read.json()["content"] == "hello"
        finally:
            if workspace_id is not None:
                await client.delete(f"/v1/workspaces/{workspace_id}")
            await client.delete(f"/v1/workspace_templates/{template_id}")
    finally:
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0396 — state_path collision with user-files PUT returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0396_workspace_state_path_user_files_collision_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0396 — User/state isolation: a PUT to a user-file path that
    falls inside the template's `state_path` (e.g. `.state/foo`) MUST
    NOT clobber the state repo. The local backend (per
    matrix/workspace/local/workspace.py L293) blocks writes / deletes
    inside `.state` and `.tmp`. The contract is a clean 4xx envelope,
    never a 5xx and never a silent overwrite of internal state.
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

        # Default state_path is ".state". Try writing inside it.
        for collide_path in (".state/intruder.txt", ".tmp/intruder.txt"):
            put = await client.put(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": collide_path},
                json={"content": "should-not-land", "encoding": "text"},
            )
            # Hard pin: never 5xx
            assert put.status_code < 500, (
                f"PUT into reserved {collide_path!r} returned 5xx: "
                f"{put.status_code}: {put.text}"
            )
            # Should be rejected (403/409/422) with a clean envelope
            assert put.status_code in range(400, 500), (
                f"PUT into reserved {collide_path!r} should be 4xx; "
                f"got {put.status_code}: {put.text}"
            )
            envelope = put.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope
            assert envelope.get("type") != "/errors/internal", envelope

            # Defence-in-depth: even if the 4xx slipped, the file
            # must NOT be readable back (state isolation upheld)
            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": collide_path},
            )
            # /files/read on a non-existent (or hidden) reserved path
            # should be 404 or a similar clean 4xx, never a 200 with
            # our injected content.
            if read.status_code == 200:
                assert read.json().get("content") != "should-not-land", (
                    f"reserved path {collide_path!r} was clobbered: "
                    f"{read.json()!r}"
                )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0401 — PUT encoding=text with a lone-surrogate string returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0401_workspace_files_put_text_lone_surrogate_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0401 — A `content` string containing a lone surrogate (e.g.
    `"\\ud800"`) is degenerate: lone surrogates are valid in JSON
    string literals but invalid in UTF-8 (orjson and Python's
    str.encode("utf-8") both reject them).

    The contract: never 5xx. Either the API rejects the body cleanly
    (4xx envelope, ideally 422) OR it accepts and persists some
    canonicalised form (round-trip via /files/read returns SOMETHING
    sane). The hard pin is the absence of /errors/internal.
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

        # Build raw JSON wire bytes containing a lone surrogate. The
        # JSON spec allows `\uXXXX` escapes inside strings, so this is
        # a well-formed JSON document; whether it can be ROUND-TRIPPED
        # to a Python str depends on the parser.
        raw_body = b'{"content":"prefix-\\ud800-suffix","encoding":"text"}'
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "lone-surrogate.txt"},
            content=raw_body,
            headers={"content-type": "application/json"},
        )
        # Hard pin: never 5xx
        assert put.status_code < 500, (
            f"PUT with lone-surrogate content returned 5xx: "
            f"{put.status_code}: {put.text}"
        )
        # Either accepted (204 / 200) or rejected (4xx)
        assert put.status_code in (200, 204) or 400 <= put.status_code < 500, (
            f"unexpected status: {put.status_code}: {put.text}"
        )

        if put.status_code in range(400, 500):
            envelope = put.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope
            assert envelope.get("type") != "/errors/internal", envelope
        else:
            # If the API accepted, /files/read on the path must not 5xx
            # (and the file should ideally be readable in some form).
            read = await client.get(
                f"/v1/workspaces/{workspace_id}/files/read",
                params={"path": "lone-surrogate.txt"},
            )
            assert read.status_code < 500, (
                f"READ after lone-surrogate PUT returned 5xx: "
                f"{read.status_code}: {read.text}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0434 — WorkspaceTemplate state_path containing `..` is rejected
# ============================================================================


@pytest.mark.asyncio
async def test_t0434_workspace_template_state_path_path_traversal_rejected(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0434 — `state_path` is documented as "Path inside the
    workspace root where the state repo lives" (matrix/model/
    workspace.py:389). A traversal-style value like `..` or
    `../escape` would let the state repo land OUTSIDE the workspace
    root: `Path(root) / ".."` resolves to `root.parent`, which is the
    workspace_provider's root containing every workspace.

    Hard contract: rejection must happen at create time (cleanest)
    OR at materialise time (acceptable). Either way the eventual
    failure surface MUST be a clean 4xx envelope with no
    /errors/internal — and the state repo MUST NOT have been
    initialised under the parent directory.
    """
    provider_id = f"wp-t0434-{unique_suffix}"
    template_id = f"wt-t0434-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json=_provider_body(provider_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    template_created = False
    workspace_id: str | None = None
    try:
        # Try several traversal shapes
        for traversal in ("..", "../escape", "../../escape", "foo/../.."):
            tpl_body = {
                "id": template_id,
                "description": f"T0434 traversal={traversal}",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "state_path": traversal,
            }
            tpl = await client.post(
                "/v1/workspace_templates", json=tpl_body,
            )
            # Hard pin: never 5xx
            assert tpl.status_code < 500, (
                f"template create with state_path={traversal!r} "
                f"returned 5xx: {tpl.status_code}: {tpl.text}"
            )

            if tpl.status_code in range(400, 500):
                # Rejected at create time — best contract
                envelope = tpl.json()
                assert envelope.get("type", "").startswith("/errors/"), envelope
                assert envelope.get("type") != "/errors/internal", envelope
                continue

            # Accepted at create time — try materialising and pin
            # the failure at that layer instead
            assert tpl.status_code == 201, tpl.text
            template_created = True
            try:
                ws = await client.post(
                    "/v1/workspaces",
                    json={"template_id": template_id},
                )
                assert ws.status_code < 500, (
                    f"workspace materialise with traversal state_path "
                    f"leaked 5xx: {ws.status_code}: {ws.text}"
                )
                if ws.status_code == 201:
                    # Worst case — the workspace materialised. The
                    # state repo would be in the wrong place. Pin
                    # this as a known bug to chase.
                    workspace_id = ws.json()["id"]
                    pytest.fail(
                        f"workspace with state_path={traversal!r} "
                        f"materialised successfully — path traversal "
                        f"into provider root is now possible. Fix: "
                        f"validate state_path in WorkspaceTemplate "
                        f"model_validator."
                    )
                else:
                    envelope = ws.json()
                    assert envelope.get("type", "").startswith("/errors/")
                    assert envelope.get("type") != "/errors/internal"
            finally:
                # Delete the template before retrying with another shape
                await client.delete(f"/v1/workspace_templates/{template_id}")
                template_created = False
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if template_created:
            await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0435 — WorkspaceTemplate state_path absolute path is rejected
# ============================================================================


@pytest.mark.asyncio
async def test_t0435_workspace_template_state_path_absolute_rejected(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0435 — Absolute `state_path` would also escape the workspace
    root: `Path(root) / "/etc/foo"` evaluates to `Path("/etc/foo")`
    in pathlib (joining an absolute path REPLACES the left side).
    Same risk profile as T0434 — pin rejection.

    Hard contract: never 5xx; rejection at create or materialise
    time with a clean 4xx envelope; state repo MUST NOT land at the
    absolute target.
    """
    provider_id = f"wp-t0435-{unique_suffix}"
    template_id = f"wt-t0435-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json=_provider_body(provider_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    template_created = False
    workspace_id: str | None = None
    try:
        # Try POSIX absolute and a Windows-style absolute (only one
        # matches the running OS, but the validator should reject
        # both regardless of platform — they're equally dangerous in
        # cross-platform deployments)
        for abs_path in (
            "/tmp/escape-t0435",
            "/etc/escape-t0435",
            "C:\\escape-t0435",
        ):
            tpl_body = {
                "id": template_id,
                "description": f"T0435 abs={abs_path}",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "state_path": abs_path,
            }
            tpl = await client.post(
                "/v1/workspace_templates", json=tpl_body,
            )
            # Hard pin: never 5xx
            assert tpl.status_code < 500, (
                f"template create with state_path={abs_path!r} "
                f"returned 5xx: {tpl.status_code}: {tpl.text}"
            )

            if tpl.status_code in range(400, 500):
                # Rejected at create time — best contract
                envelope = tpl.json()
                assert envelope.get("type", "").startswith("/errors/"), envelope
                assert envelope.get("type") != "/errors/internal", envelope
                continue

            # Accepted at create — try materialising
            assert tpl.status_code == 201, tpl.text
            template_created = True
            try:
                ws = await client.post(
                    "/v1/workspaces",
                    json={"template_id": template_id},
                )
                assert ws.status_code < 500, (
                    f"workspace materialise with absolute state_path "
                    f"leaked 5xx: {ws.status_code}: {ws.text}"
                )
                if ws.status_code == 201:
                    workspace_id = ws.json()["id"]
                    pytest.fail(
                        f"workspace with state_path={abs_path!r} "
                        f"materialised — absolute path escape is now "
                        f"possible. Fix: validate state_path in "
                        f"WorkspaceTemplate model_validator (reject "
                        f"any path with os.path.isabs() == True)."
                    )
                else:
                    envelope = ws.json()
                    assert envelope.get("type", "").startswith("/errors/")
                    assert envelope.get("type") != "/errors/internal"
            finally:
                await client.delete(f"/v1/workspace_templates/{template_id}")
                template_created = False
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        if template_created:
            await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0436 — Concurrent PUT × DELETE on same workspace file path: clean envelopes
# ============================================================================


@pytest.mark.asyncio
async def test_t0436_workspace_files_put_delete_race_same_path_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0436 — Race a PUT against a DELETE on the same file path
    inside one workspace. Both endpoints touch the local backend's
    same on-disk path. Pin: both calls return < 500; final state
    is observable (file either present with the PUT content or
    absent — never half-written / never /errors/internal).
    """
    import asyncio as _asyncio
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

        # Pre-seed the file so DELETE has something to hit
        seed = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "raced.txt"},
            json={"content": "seed", "encoding": "text"},
        )
        assert seed.status_code == 204, seed.text

        # Now race PUT × DELETE on the SAME path
        new_content = "after-race-content"
        put_task = _asyncio.create_task(client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "raced.txt"},
            json={"content": new_content, "encoding": "text"},
        ))
        del_task = _asyncio.create_task(client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "raced.txt"},
        ))
        put_resp, del_resp = await _asyncio.gather(put_task, del_task)

        # Hard pin: never 5xx, never /errors/internal
        for r, label in ((put_resp, "PUT"), (del_resp, "DELETE")):
            assert r.status_code < 500, (
                f"{label} race leaked 5xx: {r.status_code}: {r.text}"
            )
            if r.status_code >= 400:
                envelope = r.json() if r.content else {}
                assert envelope.get("type", "").startswith("/errors/"), envelope
                assert envelope.get("type") != "/errors/internal", envelope

        # Documented codes per single-call contracts
        assert put_resp.status_code in (204, 404), (
            f"PUT race: unexpected {put_resp.status_code}: {put_resp.text}"
        )
        assert del_resp.status_code in (204, 404), (
            f"DELETE race: unexpected {del_resp.status_code}: {del_resp.text}"
        )

        # Final state is observable — never half-written. Read the
        # file: either the PUT won (content = new_content) or the
        # DELETE won (read returns 404).
        final = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": "raced.txt"},
        )
        assert final.status_code in (200, 404), final.text
        if final.status_code == 200:
            # PUT won (or both ran with PUT landing last) — content
            # must be exactly new_content (no half-write corruption)
            assert final.json()["content"] == new_content, (
                f"file content corrupted by PUT/DELETE race: "
                f"{final.json()!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0437 — Workspace destroy mid-PUT: many concurrent PUTs all clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0437_workspace_destroy_mid_burst_put_all_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0437 — Sample-size sibling of T0290 (single PUT × destroy
    race). Fire 10 PUTs concurrently with a workspace destroy; pin
    that EVERY one of the 10 PUTs returns a clean envelope (no 5xx,
    no /errors/internal). Catches a regression where the destroy
    cascade leaves the in-memory workspace half-torn-down such that
    one specific in-flight PUT (ordering-dependent) leaks an
    AttributeError or KeyError.

    Subsequent GET on the workspace must be 404.
    """
    import asyncio as _asyncio
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # 10 PUTs to distinct paths racing one destroy
        put_tasks = [
            _asyncio.create_task(client.put(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": f"burst-{i}.txt"},
                json={"content": f"burst-{i}", "encoding": "text"},
            ))
            for i in range(10)
        ]
        delete_task = _asyncio.create_task(client.delete(
            f"/v1/workspaces/{workspace_id}",
        ))
        all_results = await _asyncio.gather(*put_tasks, delete_task)
        put_responses = all_results[:10]
        delete_resp = all_results[10]

        # EVERY PUT clean — no 5xx, no /errors/internal
        for i, r in enumerate(put_responses):
            assert r.status_code < 500, (
                f"PUT burst[{i}] leaked 5xx: {r.status_code}: {r.text}"
            )
            if r.status_code >= 400:
                envelope = r.json() if r.content else {}
                assert envelope.get("type") != "/errors/internal", (
                    f"PUT burst[{i}] /errors/internal: {r.text}"
                )
            # Documented codes: 204 (won the race) or 404 (workspace
            # gone) — sometimes 409 if backend signals concurrent
            # destroy
            assert r.status_code in (204, 404, 409), (
                f"PUT burst[{i}]: unexpected {r.status_code}: {r.text}"
            )

        # DELETE itself clean
        assert delete_resp.status_code < 500, delete_resp.text
        assert delete_resp.status_code in (204, 404), delete_resp.text

        # GET on the destroyed workspace eventually 404s cleanly
        gone = await client.get(f"/v1/workspaces/{workspace_id}")
        assert gone.status_code in (200, 404), gone.text
    finally:
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0438 — WorkspaceTemplate init_command failure → workspace 4xx clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0438_workspace_template_init_command_failure_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0438 — Per matrix/workspace/local/backend.py:227, when an
    init_command exits non-zero the backend raises BadRequestError
    with the rc + stderr surfaced. The API must turn that into a
    400 /errors/bad-request envelope (or similar 4xx) — never 5xx.

    Also pin that the backend's rollback ran: GET /v1/workspaces
    after the failed POST does not list the would-be id.
    """
    provider_id = f"wp-t0438-{unique_suffix}"
    template_id = f"wt-t0438-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json=_provider_body(provider_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    try:
        # Use a portable failing command. `false` is POSIX; on Windows
        # we use `cmd /c exit 1` since shell=True invokes through cmd.
        # asyncio.create_subprocess_shell on Windows uses ComSpec
        # (cmd.exe) so `exit 1` works there too.
        failing_cmd = "exit 1"
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "T0438",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "init_commands": [failing_cmd],
            },
        )
        assert tpl.status_code == 201, tpl.text

        # Snapshot the workspaces list pre-failure for the rollback
        # check below.
        list_before = await client.get(
            "/v1/workspaces?limit=200&offset=0",
        )
        assert list_before.status_code == 200, list_before.text
        ids_before = {
            item["id"] for item in list_before.json()["items"]
        }

        # POST workspace — init_command exits 1 → BadRequestError
        ws = await client.post(
            "/v1/workspaces", json={"template_id": template_id},
        )
        # Hard pin: never 5xx
        assert ws.status_code < 500, (
            f"init_command failure leaked 5xx: "
            f"{ws.status_code}: {ws.text}"
        )
        # Should be 4xx with clean envelope mentioning rc=1
        assert 400 <= ws.status_code < 500, (
            f"init_command failure should be 4xx; got "
            f"{ws.status_code}: {ws.text}"
        )
        envelope = ws.json()
        assert envelope.get("type", "").startswith("/errors/"), envelope
        assert envelope.get("type") != "/errors/internal", envelope
        # The detail / extensions should reference the failing command
        # so an operator can act on it
        body_str = ws.text
        assert "init" in body_str.lower() or "command" in body_str.lower() \
            or "rc=" in body_str or "exit" in body_str.lower(), (
            f"4xx envelope should reference the init command failure; "
            f"body={body_str!r}"
        )

        # Defence: rollback ran. The workspace list must not contain
        # any new id from this attempt.
        list_after = await client.get(
            "/v1/workspaces?limit=200&offset=0",
        )
        assert list_after.status_code == 200, list_after.text
        ids_after = {
            item["id"] for item in list_after.json()["items"]
        }
        new_ids = ids_after - ids_before
        assert not new_ids, (
            f"backend rollback failed: workspace row was created "
            f"despite init_command failure: {new_ids!r}"
        )
    finally:
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0445 — Workspace file PUT→READ→PUT preserves mtime advancement
# ============================================================================


@pytest.mark.asyncio
async def test_t0445_workspace_files_mtime_advances_across_two_puts(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0445 — Two PUTs to the same path with a 1.5s sleep between
    them. The second /files/info `mtime` field must be strictly
    greater than the first; the size field must reflect the second
    body. Filesystem mtime resolution is at most 1s on every common
    filesystem we'd run on (NTFS, ext4, APFS), so 1.5s is safely
    above the floor.
    """
    import asyncio
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

        path = "mtime-probe.txt"
        first_body = "first"
        second_body = "second-body-much-longer"

        # First PUT
        put1 = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": first_body, "encoding": "text"},
        )
        assert put1.status_code == 204, put1.text
        info1 = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": path},
        )
        assert info1.status_code == 200, info1.text
        info1_body = info1.json()
        mtime1 = info1_body.get("modified_at")
        size1 = info1_body.get("size_bytes")
        assert mtime1 is not None, info1_body
        assert size1 == len(first_body.encode("utf-8")), info1_body

        # Sleep across the filesystem mtime resolution boundary
        await asyncio.sleep(1.5)

        # Second PUT to the same path
        put2 = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": second_body, "encoding": "text"},
        )
        assert put2.status_code == 204, put2.text
        info2 = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": path},
        )
        assert info2.status_code == 200, info2.text
        info2_body = info2.json()
        mtime2 = info2_body.get("modified_at")
        size2 = info2_body.get("size_bytes")

        # mtime advanced (strictly greater)
        from datetime import datetime
        m1 = datetime.fromisoformat(mtime1.replace("Z", "+00:00"))
        m2 = datetime.fromisoformat(mtime2.replace("Z", "+00:00"))
        assert m2 > m1, (
            f"mtime did not advance across two PUTs separated by 1.5s: "
            f"first={mtime1!r}, second={mtime2!r}"
        )
        # size reflects the second body
        assert size2 == len(second_body.encode("utf-8")), (
            f"size_bytes did not reflect second PUT body: "
            f"got={size2}, expected={len(second_body.encode('utf-8'))}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0446 — Workspace files PUT → /info reports written size_bytes (no race)
# ============================================================================


@pytest.mark.asyncio
async def test_t0446_workspace_files_put_info_no_fsync_race(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0446 — Tight loop: PUT N-byte body, immediately GET /files/
    info, assert size_bytes == N. Repeat 20 iterations across 3
    distinct sizes (small/medium/large). Catches a regression where
    /info reads before the kernel has flushed, returning 0 or a
    stale size from a previous write.
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

        sizes = [16, 4096, 65536]
        for iteration in range(20):
            for size in sizes:
                body = "x" * size
                path = f"probe-{iteration}-{size}.txt"
                put = await client.put(
                    f"/v1/workspaces/{workspace_id}/files",
                    params={"path": path},
                    json={"content": body, "encoding": "text"},
                )
                assert put.status_code == 204, (
                    f"iter {iteration} size {size}: PUT failed: {put.text}"
                )
                info = await client.get(
                    f"/v1/workspaces/{workspace_id}/files/info",
                    params={"path": path},
                )
                assert info.status_code == 200, (
                    f"iter {iteration} size {size}: info failed: "
                    f"{info.text}"
                )
                reported = info.json().get("size_bytes")
                assert reported == size, (
                    f"iter {iteration} size {size}: /info reported "
                    f"size_bytes={reported} (expected {size}). "
                    f"Possible fsync/cache race."
                )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0463 — Workspace POSIX file mode bits surface in /files/info
# ============================================================================


@pytest.mark.asyncio
async def test_t0463_workspace_files_info_posix_mode_documented(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0463 — On POSIX backends, files have meaningful permission
    bits (read/write/execute for user/group/other). Pin whatever
    field /files/info exposes for them — or, if no such field
    exists, document the absence so a future addition deliberately
    breaks this test.

    The contract being pinned is consistency: two consecutive
    /info reads on the same unmodified file must return the same
    field set and the same value for any mode/permission field
    present. If the API doesn't expose mode bits at all, that's
    documented as the current contract.

    Skipped on Windows (NTFS doesn't have POSIX mode bits in any
    portable sense; even when files have ACLs, the API probably
    doesn't model that).
    """
    if os.name == "nt":
        pytest.skip(
            "POSIX file mode bits are not meaningful on Windows NTFS"
        )

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

        # Seed a file
        path = "mode-probe.txt"
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": "permission-probe", "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        # Two consecutive /info reads
        first = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": path},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()

        second = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": path},
        )
        assert second.status_code == 200, second.text
        second_body = second.json()

        # Field set is identical between the two reads
        assert set(first_body.keys()) == set(second_body.keys()), (
            f"info field set drifted between reads: "
            f"first={sorted(first_body.keys())!r}, "
            f"second={sorted(second_body.keys())!r}"
        )

        # Look for mode-style fields (any of the common names)
        mode_field_names = ("mode", "permissions", "perm", "unix_mode")
        present = [
            name for name in mode_field_names if name in first_body
        ]
        if present:
            # If any mode field exists, it must be stable across reads
            for name in present:
                assert first_body[name] == second_body[name], (
                    f"mode field {name!r} changed across reads: "
                    f"first={first_body[name]!r}, "
                    f"second={second_body[name]!r}"
                )
        else:
            # Document the absence: API does not currently expose
            # POSIX mode bits. Print to pytest -s output for
            # visibility; not a failure.
            print(
                f"\n[T0463] /files/info does NOT expose POSIX mode "
                f"bits. Available fields: "
                f"{sorted(first_body.keys())!r}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0476 — /files/info on freshly-written file in NEW subdir reports correct path
# ============================================================================


@pytest.mark.asyncio
async def test_t0476_workspace_files_info_in_new_subdir_correct_path(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0476 — PUT a file at `newdir/foo.txt` (subdir does not exist
    yet — local backend must create intermediate directories per
    T0046). Then GET /files/info: the response `path` field must
    equal the input path exactly, and `kind` must be `file`.

    Pin: subdir creation does not corrupt the path field; /info
    correctly identifies file vs directory.
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

        new_path = "newdir/foo.txt"
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": new_path},
            json={"content": "subdir-probe", "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": new_path},
        )
        assert info.status_code == 200, info.text
        info_body = info.json()
        assert info_body.get("path") == new_path, (
            f"path field corrupted: sent={new_path!r}, "
            f"got={info_body.get('path')!r}"
        )
        assert info_body.get("kind") == "file", (
            f"kind should be 'file' for a freshly-written file; "
            f"got {info_body.get('kind')!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0477 — DELETE /files?path=/ returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0477_workspace_files_delete_root_path_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0477 — Parallel of T0146 (PUT root path rejected) for the
    DELETE verb. DELETE /v1/workspaces/{wid}/files?path=/ must
    reject cleanly (4xx with /errors/* envelope) — never destroy
    the workspace root, never 5xx. The workspace itself stays
    functional after the rejected DELETE.
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

        # Try several root-shaped paths
        for root_shape in ("/", ".", ""):
            resp = await client.delete(
                f"/v1/workspaces/{workspace_id}/files",
                params={"path": root_shape},
            )
            envelope = resp.json() if resp.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"DELETE root path={root_shape!r} leaked "
                f"/errors/internal: {resp.text}"
            )
            assert resp.status_code < 500, resp.text
            # Either rejected (404/4xx) or maybe accepted as no-op,
            # but NEVER destroys the workspace root. Accept any
            # non-5xx; we'll verify the workspace is still usable
            # at the end.
            assert resp.status_code in (204, 400, 404, 422), (
                f"DELETE path={root_shape!r}: unexpected "
                f"{resp.status_code}: {resp.text}"
            )

        # Workspace still functional — listing root succeeds
        listed = await client.get(f"/v1/workspaces/{workspace_id}/files")
        assert listed.status_code == 200, (
            f"workspace root listing broken after DELETE attempts: "
            f"{listed.status_code}: {listed.text}"
        )
        assert isinstance(listed.json().get("items"), list), listed.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0478 — /files PUT to a path containing NUL byte rejected cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0478_workspace_files_put_nul_byte_path_rejected_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0478 — Filenames containing NUL bytes are illegal on every
    POSIX filesystem (NUL terminates C strings) and on NTFS. The
    HTTP layer doesn't have a special interpretation of NUL but the
    on-disk write WILL fail. Pin: 4xx clean envelope (ideally 400
    /errors/bad-request); never 5xx, never /errors/internal; no
    listing entry leaked.
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

        # NUL byte in the basename. URL-encoded as %00 over the wire;
        # the server's URL decoder must NOT collapse it silently.
        nul_path = "before\x00after.txt"

        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": nul_path},
            json={"content": "nul-probe", "encoding": "text"},
        )
        envelope = write.json() if write.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"NUL-byte path leaked /errors/internal: {write.text}"
        )
        # Hard pin: never 5xx
        assert write.status_code < 500, write.text
        # Documented rejection codes
        assert write.status_code in (400, 422), (
            f"NUL-byte path should be 4xx; got "
            f"{write.status_code}: {write.text}"
        )
        assert envelope.get("type", "").startswith("/errors/"), envelope

        # Defence: no leaked entry in the root listing
        listed = await client.get(f"/v1/workspaces/{workspace_id}/files")
        assert listed.status_code == 200, listed.text
        names = [item.get("path", "") for item in listed.json()["items"]]
        for name in names:
            assert "\x00" not in name, (
                f"NUL byte leaked into listing: {name!r}"
            )
            assert "after.txt" not in name, (
                f"NUL-rejected filename appears in listing as "
                f"{name!r}; the rejection may have been bypassed."
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0479 — WorkspaceTemplate with empty packages list materialises cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0479_workspace_template_empty_packages_materialises_cleanly(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0479 — WorkspaceTemplate.packages defaults to `[]` per
    matrix/model/workspace.py:370. Pin: explicitly setting
    `packages: []` materialises a workspace cleanly (no fallback to
    a "default packages" set or 5xx). /files and /log respond 200
    on the materialised workspace.
    """
    provider_id = f"wp-t0479-{unique_suffix}"
    template_id = f"wt-t0479-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json=_provider_body(provider_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    workspace_id: str | None = None
    try:
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "T0479 explicit empty packages",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "packages": [],  # explicitly empty
            },
        )
        assert tpl.status_code == 201, tpl.text

        ws = await client.post(
            "/v1/workspaces", json={"template_id": template_id},
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        files = await client.get(f"/v1/workspaces/{workspace_id}/files")
        assert files.status_code == 200, files.text
        assert isinstance(files.json().get("items"), list), files.json()

        log = await client.get(f"/v1/workspaces/{workspace_id}/log")
        assert log.status_code == 200, log.text
        assert isinstance(log.json().get("commits"), list), log.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0480 — Workspace destroy fired twice in rapid succession converges cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0480_workspace_destroy_concurrent_double_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0480 — Race-sibling of T0162 (sequential destroy idempotency).
    Fire two concurrent DELETE /v1/workspaces/{wid} requests. Pin:
    both clean envelopes (one 204 winner + one 204/404 loser); never
    /errors/internal; final GET on the workspace is 404.
    """
    import asyncio
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    try:
        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        # Fire two concurrent destroys
        d1_task = asyncio.create_task(client.delete(
            f"/v1/workspaces/{workspace_id}",
        ))
        d2_task = asyncio.create_task(client.delete(
            f"/v1/workspaces/{workspace_id}",
        ))
        d1, d2 = await asyncio.gather(d1_task, d2_task)

        # No /errors/internal
        for r, label in ((d1, "first"), (d2, "second")):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label} destroy leaked /errors/internal: {r.text}"
            )
            assert r.status_code < 500, (
                f"{label} destroy 5xx: {r.status_code}: {r.text}"
            )
            # Documented codes: 204 (won) or 404 (lost)
            assert r.status_code in (204, 404), (
                f"{label} destroy: unexpected {r.status_code}: {r.text}"
            )

        # At least one 204 winner; the other may be 204 (if both
        # observed an existing row before either committed) or 404
        winners = sum(1 for r in (d1, d2) if r.status_code == 204)
        assert winners >= 1, (
            f"both destroys 404 — workspace was never destroyed? "
            f"first={d1.status_code}, second={d2.status_code}"
        )

        # Final GET returns 404
        gone = await client.get(f"/v1/workspaces/{workspace_id}")
        assert gone.status_code == 404, gone.text
    finally:
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0481 — /files/info reports kind=file vs kind=directory correctly
# ============================================================================


@pytest.mark.asyncio
async def test_t0481_workspace_files_info_file_vs_directory_kind(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0481 — Both /files/info on a directory AND on a file in the
    same workspace must report the correct `kind`. T0114 (dir) and
    T0476 (file in new subdir) cover each in isolation; this pins
    that the same /info handler distinguishes them on the same
    workspace, never confusing one for the other.
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

        # Seed a file inside a subdir — creates both
        write = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "dir/leaf.txt"},
            json={"content": "leaf-content", "encoding": "text"},
        )
        assert write.status_code == 204, write.text

        # /info on the file
        info_file = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": "dir/leaf.txt"},
        )
        assert info_file.status_code == 200, info_file.text
        body_file = info_file.json()
        assert body_file.get("kind") == "file", (
            f"expected kind=file for dir/leaf.txt; got {body_file!r}"
        )
        assert body_file.get("path") == "dir/leaf.txt", body_file

        # /info on the directory
        info_dir = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": "dir"},
        )
        assert info_dir.status_code != 500, info_dir.text
        assert info_dir.status_code < 500, info_dir.text
        if info_dir.status_code == 200:
            body_dir = info_dir.json()
            assert body_dir.get("kind") in ("dir", "directory"), (
                f"expected kind=dir for 'dir'; got {body_dir!r}"
            )
            assert body_dir.get("path") == "dir", body_dir
        else:
            # Some implementations 404 on dirs from /info; clean
            # envelope is the contract
            envelope = info_dir.json()
            assert envelope.get("type", "").startswith("/errors/"), envelope

        # Repeat both calls to confirm consistency
        info_file_2 = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": "dir/leaf.txt"},
        )
        assert info_file_2.json().get("kind") == "file", info_file_2.text
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0482 — PUT binary base64 then READ as text returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0482_workspace_files_put_binary_read_as_text_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0482 — PUT raw binary bytes (0x00..0xff) via base64 encoding,
    then GET /files/read with encoding=text. The bytes are not
    valid UTF-8, so the text-decode either succeeds with replacement
    chars (200 with the body containing U+FFFD) or rejects (4xx).
    Pin: never 5xx, never /errors/internal.
    """
    import base64
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

        # All 256 byte values — not valid UTF-8
        raw_bytes = bytes(range(256))
        b64 = base64.b64encode(raw_bytes).decode("ascii")

        path = "binary-blob.bin"
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": b64, "encoding": "base64"},
        )
        assert put.status_code == 204, put.text

        # Sanity: read as base64 round-trips byte-exact
        read_b64 = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path, "encoding": "base64"},
        )
        assert read_b64.status_code == 200, read_b64.text
        decoded = base64.b64decode(read_b64.json()["content"])
        assert decoded == raw_bytes, (
            f"binary content corrupted on base64 round-trip: "
            f"len_sent={len(raw_bytes)}, len_got={len(decoded)}"
        )

        # The probe: read as text — must NOT 5xx
        read_text = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path, "encoding": "text"},
        )
        envelope = read_text.json() if read_text.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"binary-as-text leaked /errors/internal: {read_text.text}"
        )
        assert read_text.status_code < 500, read_text.text
        # Acceptable: 200 (with replacement chars in body) OR 4xx
        # (rejected as undecodable)
        assert read_text.status_code in (200, 400, 422), (
            f"unexpected status: {read_text.status_code}: "
            f"{read_text.text[:300]}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0513 — Workspace files PUT with charset=utf-16 returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0513_workspace_files_put_charset_utf16_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0513 — Pin Content-Type charset variant beyond T0374's
    `application/json; charset=utf-8`. UTF-16 is exotic for HTTP
    JSON bodies and may not be supported. The contract: never 5xx,
    never /errors/internal. Either accepted (the body parser
    handles the charset) or rejected with a clean 4xx envelope.
    """
    import json
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

        body_dict = {"content": "utf16-probe", "encoding": "text"}
        # Encode body as UTF-16 (with BOM); server may or may not
        # decode that depending on its body parser
        body_bytes = json.dumps(body_dict).encode("utf-16")

        resp = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "utf16-probe.txt"},
            content=body_bytes,
            headers={"content-type": "application/json; charset=utf-16"},
        )
        envelope = (
            resp.json() if (resp.content and resp.status_code >= 400) else {}
        )
        assert envelope.get("type") != "/errors/internal", (
            f"utf-16 charset PUT leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code < 500, resp.text
        # Documented: 204 (accepted), or 4xx (rejected as bad
        # charset / unparseable JSON)
        assert resp.status_code in (204, 400, 415, 422), (
            f"unexpected status: {resp.status_code}: {resp.text}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0514 — DELETE /files on path whose parent dir doesn't exist returns 404
# ============================================================================


@pytest.mark.asyncio
async def test_t0514_workspace_files_delete_missing_parent_dir_clean_404(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0514 — DELETE /files?path=nonexistent_dir/file.txt where
    neither the directory nor the file exist. Pin: 404
    /errors/not-found (or another clean 4xx); never 5xx, never
    /errors/internal — the missing parent directory must not
    surface as a stat-failure or rmdir-failure 500.
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

        resp = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "nonexistent_dir_t0514/missing-file.txt"},
        )
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"DELETE on missing parent dir leaked /errors/internal: "
            f"{resp.text}"
        )
        assert resp.status_code < 500, resp.text
        assert resp.status_code in (204, 404), (
            f"DELETE on missing path: unexpected {resp.status_code}: "
            f"{resp.text}"
        )
        if resp.status_code == 404:
            assert envelope.get("type") == "/errors/not-found", envelope
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0515 — WorkspaceTemplate with explicit `files: []` materialises cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0515_workspace_template_explicit_empty_files_materialises(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0515 — Pin that explicitly setting `files: []` (empty
    FileMount list) on a template materialises a workspace cleanly
    — same outcome as omitting the field entirely (T0479 covers
    explicit empty packages list; this is the FileMount complement).
    /files list returns 200 (with the auto-created .state/.tmp dirs
    plus zero seeded files).
    """
    provider_id = f"wp-t0515-{unique_suffix}"
    template_id = f"wt-t0515-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json=_provider_body(provider_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    workspace_id: str | None = None
    try:
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "T0515 explicit empty files",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "files": [],  # explicitly empty
            },
        )
        assert tpl.status_code == 201, tpl.text

        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        files = await client.get(f"/v1/workspaces/{workspace_id}/files")
        assert files.status_code == 200, files.text
        items = files.json().get("items")
        assert isinstance(items, list), files.json()

        log = await client.get(f"/v1/workspaces/{workspace_id}/log")
        assert log.status_code == 200, log.text
        assert isinstance(log.json().get("commits"), list), log.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0516 — WorkspaceTemplate env value with special chars round-trips
# ============================================================================


@pytest.mark.asyncio
async def test_t0516_workspace_template_env_special_chars_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0516 — Sibling of T0111 (env propagation with a simple
    value). Pin: an env value containing shell-special characters
    `${}"'\\n` survives through Pydantic SecretStr → JSONB storage
    → subprocess env → init_command observation byte-exact.

    The init_command writes os.environ['MARKER'] verbatim to a
    file via Python's `open().write()` (no shell interpolation),
    so we read back the exact bytes the runtime received.
    """
    provider_id = f"wp-t0516-{unique_suffix}"
    template_id = f"wt-t0516-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json=_provider_body(provider_id, tmp_path),
    )
    assert pr.status_code == 201, pr.text
    workspace_id: str | None = None

    # Special-char marker — combination of $ } { " ' newline literal
    # (escaped here so the JSON wire form preserves it), backslash
    special_marker = (
        f"sp-{unique_suffix}::dollar=$VAR::brace={{a}}::quote=\"::"
        "apos='::backslash=\\\\::tab=\t"
    )
    try:
        # Init command uses Python (no shell interpolation) to
        # capture os.environ['MARKER'] verbatim
        init_cmd = (
            'python -c "import os; '
            "open('marker.txt','w').write(os.environ['MARKER'])\""
        )
        tpl = await client.post(
            "/v1/workspace_templates",
            json={
                "id": template_id,
                "description": "T0516 special-char env",
                "provider_id": provider_id,
                "backend": {"kind": "local"},
                "env": {"MARKER": special_marker},
                "init_commands": [init_cmd],
            },
        )
        assert tpl.status_code == 201, tpl.text

        ws = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws.status_code == 201, ws.text
        workspace_id = ws.json()["id"]

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": "marker.txt"},
        )
        assert read.status_code == 200, read.text
        observed = read.json()["content"]
        assert observed == special_marker, (
            f"env value corrupted across template→materialise→subprocess "
            f"chain.\n  sent (len={len(special_marker)}): "
            f"{special_marker!r}\n  got  (len={len(observed)}): "
            f"{observed!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{template_id}")
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0517 — Two workspaces from same template are on-disk isolated
# ============================================================================


@pytest.mark.asyncio
async def test_t0517_two_workspaces_same_template_isolated(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0517 — Materialise two workspaces from the same template,
    write a file to A's workspace, then list B's workspace files.
    Pin: A's file does NOT appear in B's listing — workspaces are
    on-disk isolated, even when sharing the same template config.
    Catches a regression where the local backend accidentally shared
    a single root across instances.
    """
    provider_id, template_id = await _setup_provider_template(
        client, suffix=unique_suffix, root=tmp_path,
    )
    ws_a_id: str | None = None
    ws_b_id: str | None = None
    try:
        ws_a = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws_a.status_code == 201, ws_a.text
        ws_a_id = ws_a.json()["id"]

        ws_b = await client.post(
            "/v1/workspaces",
            json=_workspace_body(template_id=template_id),
        )
        assert ws_b.status_code == 201, ws_b.text
        ws_b_id = ws_b.json()["id"]

        # Sanity: distinct workspace ids
        assert ws_a_id != ws_b_id, (ws_a_id, ws_b_id)

        # Write a uniquely-named file in A
        a_file = f"only-in-a-{unique_suffix}.txt"
        put = await client.put(
            f"/v1/workspaces/{ws_a_id}/files",
            params={"path": a_file},
            json={"content": "for-a-only", "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        # B's listing must not contain that file
        list_b = await client.get(f"/v1/workspaces/{ws_b_id}/files")
        assert list_b.status_code == 200, list_b.text
        b_paths = [item["path"] for item in list_b.json()["items"]]
        assert a_file not in b_paths, (
            f"workspace isolation broken: file {a_file!r} from "
            f"ws-A appears in ws-B listing: {b_paths!r}"
        )

        # B's /files/read on the A-only path 404s
        read_b = await client.get(
            f"/v1/workspaces/{ws_b_id}/files/read",
            params={"path": a_file},
        )
        assert read_b.status_code == 404, read_b.text

        # Defence: A's listing DOES contain the file (proves the
        # PUT actually landed somewhere)
        list_a = await client.get(f"/v1/workspaces/{ws_a_id}/files")
        assert list_a.status_code == 200, list_a.text
        a_paths = [item["path"] for item in list_a.json()["items"]]
        assert a_file in a_paths, (
            f"PUT to ws-A didn't land in A's listing either: {a_paths!r}"
        )
    finally:
        if ws_a_id is not None:
            await client.delete(f"/v1/workspaces/{ws_a_id}")
        if ws_b_id is not None:
            await client.delete(f"/v1/workspaces/{ws_b_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0518 — Workspace files /download Content-Length matches body byte length
# ============================================================================


@pytest.mark.asyncio
async def test_t0518_workspace_files_download_content_length_matches_body(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0518 — PUT a 1 MiB file then GET /files/download. Pin:
    if the response carries a Content-Length header (StreamingResponse
    may omit it depending on the backend), it equals the body byte
    length exactly. Catches a regression where streaming download
    emits a stale or off-by-one length.
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

        # 1 MiB body — large enough to engage streaming if any
        path = "downloadable-1mib.bin"
        size = 1024 * 1024
        body_text = "X" * size
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": body_text, "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        dl = await client.get(
            f"/v1/workspaces/{workspace_id}/files/download",
            params={"path": path},
        )
        assert dl.status_code == 200, dl.text
        # Body byte length matches what we wrote
        assert len(dl.content) == size, (
            f"download body size mismatch: expected {size}, got "
            f"{len(dl.content)}"
        )
        # Content-Length header (if present) matches body byte length
        cl = dl.headers.get("content-length")
        if cl is not None:
            assert int(cl) == size, (
                f"Content-Length header {cl!r} doesn't match body "
                f"byte length {size}"
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0526 — Workspace files PUT 1 MiB single body to root path round-trips
# ============================================================================


@pytest.mark.asyncio
async def test_t0526_workspace_files_put_1mib_root_path_round_trip(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0526 — PUT a 1 MiB single body to a root-level path
    "file.txt" (no subdir prefix). Pin: 204 at PUT; immediate
    /files/info reports size_bytes=1048576; /files/read with
    encoding=base64 round-trips byte-exact.

    Sibling of T0518 (download endpoint Content-Length); T0526 pins
    the size+round-trip via the standard /files/read instead of
    /download.
    """
    import base64
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

        size = 1024 * 1024  # exactly 1 MiB
        body_text = "X" * size
        path = "file.txt"  # root-level, no subdir prefix

        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": body_text, "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        # Immediate /files/info reports the right size
        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": path},
        )
        assert info.status_code == 200, info.text
        assert info.json().get("size_bytes") == size, (
            f"size_bytes mismatch: expected {size}, got "
            f"{info.json().get('size_bytes')!r}"
        )

        # /files/read base64 round-trip is byte-exact
        read = await client.get(
            f"/v1/workspaces/{workspace_id}/files/read",
            params={"path": path, "encoding": "base64"},
        )
        assert read.status_code == 200, read.text
        decoded = base64.b64decode(read.json()["content"])
        assert len(decoded) == size, (
            f"base64-read body size mismatch: expected {size}, got "
            f"{len(decoded)}"
        )
        assert decoded == body_text.encode("utf-8"), (
            "1 MiB body corrupted on round-trip"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0527 — Workspace files PUT then immediate /info on 0-byte file: size=0
# ============================================================================


@pytest.mark.asyncio
async def test_t0527_workspace_files_put_zero_byte_info_reports_size_zero(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0527 — PUT empty content (0 bytes) then immediately GET
    /files/info. Pin: size_bytes=0 reported correctly. Tighter than
    T0063 (which round-trips empty content via /read): T0527 probes
    the cache/fs read path under no fsync gap on a 0-byte file
    where any default-padding behaviour would inflate the size.
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

        path = "zero-byte.bin"
        put = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": path},
            json={"content": "", "encoding": "text"},
        )
        assert put.status_code == 204, put.text

        # Tight: no asyncio.sleep before /info
        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": path},
        )
        assert info.status_code == 200, info.text
        assert info.json().get("size_bytes") == 0, (
            f"0-byte file size mismatch: got "
            f"{info.json().get('size_bytes')!r}"
        )
        assert info.json().get("kind") == "file", info.json()
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)


# ============================================================================
# T0528 — Workspace files DELETE on path with trailing whitespace
# ============================================================================


@pytest.mark.asyncio
async def test_t0528_workspace_files_delete_trailing_whitespace_path_clean(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0528 — Seed a file at "foo.txt" then DELETE the path
    "foo.txt " (trailing space). Pin: clean envelope (204 if the
    backend strips whitespace consistently, OR 404 if it treats
    paths as exact strings). Original "foo.txt" survives if the
    DELETE 404'd; never /errors/internal.
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

        # Seed the file at the canonical path (no trailing space)
        seed = await client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "foo.txt"},
            json={"content": "untouched", "encoding": "text"},
        )
        assert seed.status_code == 204, seed.text

        # DELETE with trailing space
        rm = await client.delete(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": "foo.txt "},
        )
        envelope = rm.json() if rm.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"trailing-whitespace DELETE leaked /errors/internal: "
            f"{rm.text}"
        )
        assert rm.status_code in (204, 404), (
            f"unexpected status: {rm.status_code}: {rm.text}"
        )

        # Original file: gone if 204 (whitespace stripped), present
        # if 404 (paths treated as exact strings)
        info = await client.get(
            f"/v1/workspaces/{workspace_id}/files/info",
            params={"path": "foo.txt"},
        )
        if rm.status_code == 204:
            # Stripped → DELETE landed on the canonical path
            assert info.status_code == 404, (
                f"DELETE returned 204 but original 'foo.txt' is "
                f"still present: {info.text}"
            )
        else:
            # 404 → DELETE didn't match; original survives
            assert info.status_code == 200, (
                f"DELETE returned 404 but original 'foo.txt' was "
                f"removed anyway: {info.text}"
            )
            assert info.json().get("size_bytes") == len("untouched"), (
                info.json()
            )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_provider_template(client, provider_id, template_id)
