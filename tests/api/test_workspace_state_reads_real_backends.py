"""A-22 against the real backends: the guard classifies a path the way the
backend RESOLVES it.

* LocalWorkspace resolves ``(root / path).resolve()``: an absolute path
  inside the root, and a symlink into ``.state``, both reach the state
  tree.
* SandboxWorkspace (docker / k8s) turns ``\\`` into ``/`` and anchors
  ``/x`` at the workspace root.

For each spelling the test asserts that the guard's verdict and the
backend's resolved target agree, and that a non-admin read through the
REST route is refused. The sandbox write guard gets the same check: it
compared the RAW path string, so ``./.state/x`` was writable there.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from primer.model.except_ import BadRequestError
from primer.model.workspace import (
    ContainerTemplateConfig,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.workspace.local.workspace import LocalWorkspace
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.api.test_workspace_state_reads_guarded import _app

def reserved_tree_for(ws, path):
    # Imported per call so the red run fails per test, not at collection.
    from primer.workspace.reserved import reserved_tree_for as _classify

    return _classify(ws, path)


_SECRET = b"Traceback (most recent call last): /app/primer/x.py\n"


def _seed(root: Path) -> None:
    (root / ".state" / "sessions" / "s1").mkdir(parents=True)
    (root / ".state" / "sessions" / "s1" / "messages.jsonl").write_bytes(_SECRET)
    (root / ".tmp" / "s1").mkdir(parents=True)
    (root / ".tmp" / "s1" / "tool_1.txt").write_bytes(_SECRET)
    (root / "src").mkdir()
    (root / "src" / "main.py").write_bytes(b"print(1)\n")


def _local(root: Path) -> LocalWorkspace:
    template = WorkspaceTemplate.model_validate({
        "id": "tpl", "description": "", "provider_id": "p",
        "backend": {"kind": "local"},
    })
    return LocalWorkspace(
        workspace_id="w", root=root, template=template, env={},
        state_repo=None, truncation_store=None, tools=[],  # type: ignore[arg-type]
    )


def _sandbox(root: Path) -> SandboxWorkspace:
    template = WorkspaceTemplate(
        id="tpl", description="", provider_id="p",
        backend=ContainerTemplateConfig(image="python:3.13"),
    )
    return SandboxWorkspace(
        workspace_id="w", template=template, sandbox=FakeSandbox(root=root),
        state_repo=None, truncation_store=None, tools=[],  # type: ignore[arg-type]
        backend_kind="container",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token=SecretStr("t")),
    )


async def _read_as_user(ws, path: str):
    app, registry = _app("user")
    registry.ws = ws
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(
            "/v1/workspaces/w/files/read", params={"path": path},
        )


# --------------------------------------------------------------- local ---


def _local_spellings(root: Path) -> list[str]:
    return [
        str(root / ".state" / "sessions" / "s1" / "messages.jsonl"),
        str(root) + "/src/../.state/sessions/s1/messages.jsonl",
        str(root) + "//.tmp/s1/tool_1.txt",
        "link/sessions/s1/messages.jsonl",
    ]


@pytest.fixture
def local_ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    _seed(root)
    (root / "link").symlink_to(root / ".state")
    return _local(root), root


@pytest.mark.parametrize("i", range(4))
async def test_local_guard_and_backend_agree(local_ws, i):
    ws, root = local_ws
    path = _local_spellings(root)[i]
    resolved = ws._resolve_path(path)
    state, tmp = (root / ".state").resolve(), (root / ".tmp").resolve()
    lands_reserved = any(
        resolved == r or r in resolved.parents for r in (state, tmp)
    )
    assert lands_reserved, "premise: the backend reads the reserved tree"
    assert reserved_tree_for(ws, path) is not None


@pytest.mark.parametrize("i", range(4))
async def test_local_user_read_of_a_reserved_target_is_refused(local_ws, i):
    ws, root = local_ws
    resp = await _read_as_user(ws, _local_spellings(root)[i])
    assert resp.status_code == 403, resp.text
    assert b"Traceback" not in resp.content


async def test_local_user_reads_their_own_file_by_absolute_path(local_ws):
    ws, root = local_ws
    resp = await _read_as_user(ws, str(root / "src" / "main.py"))
    assert resp.status_code == 200, resp.text
    assert reserved_tree_for(ws, "src/main.py") is None


# ------------------------------------------------------------- sandbox ---

_SANDBOX_SPELLINGS = [
    ".state\\sessions\\s1\\messages.jsonl",
    "src\\..\\.state\\sessions\\s1\\messages.jsonl",
    "/.state/sessions/s1/messages.jsonl",
    ".\\.tmp\\s1\\tool_1.txt",
]


@pytest.fixture
def sandbox_ws(tmp_path):
    _seed(tmp_path)
    return _sandbox(tmp_path)


@pytest.mark.parametrize("path", _SANDBOX_SPELLINGS)
def test_sandbox_guard_and_backend_agree(sandbox_ws, path):
    resolved = sandbox_ws._resolve_path(path)
    lands_reserved = resolved.startswith(("/workspace/.state/", "/workspace/.tmp/"))
    assert lands_reserved, "premise: the backend reads the reserved tree"
    assert reserved_tree_for(sandbox_ws, path) is not None


@pytest.mark.parametrize("path", _SANDBOX_SPELLINGS)
async def test_sandbox_user_read_of_a_reserved_target_is_refused(sandbox_ws, path):
    resp = await _read_as_user(sandbox_ws, path)
    assert resp.status_code == 403, resp.text
    assert b"Traceback" not in resp.content


async def test_sandbox_user_reads_their_own_file(sandbox_ws):
    resp = await _read_as_user(sandbox_ws, "src\\main.py")
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "path", ["./.state/x", "a/../.state/x", ".state\\x", "/.tmp/x", ".tmp//x"],
)
async def test_sandbox_write_guard_refuses_every_spelling(sandbox_ws, tmp_path, path):
    """The sandbox write guard compared the raw string: './.state/x' passed
    it and the resolver then wrote into .state."""
    with pytest.raises(BadRequestError):
        await sandbox_ws.write_file(path, b"pwned")
    assert not (tmp_path / ".state" / "x").exists()
    assert not (tmp_path / ".tmp" / "x").exists()


# ------------------------------------------ local delete / move ordering ---
# LocalWorkspace.delete_file / move_file checked existence (404) before the
# reserved refusal (400): a missing .state file answered 404 and an existing
# one 400, which told a caller whether it exists. Both now answer the same.

_EXISTING = ".state/sessions/s1/messages.jsonl"
_MISSING = ".state/sessions/nope/messages.jsonl"


async def _delete_as_user(ws, path: str):
    app, registry = _app("user")
    registry.ws = ws
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.delete("/v1/workspaces/w/files", params={"path": path})


async def _move_as_user(ws, src: str, dst: str):
    app, registry = _app("user")
    registry.ws = ws
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(
            "/v1/workspaces/w/files/move", params={"src": src, "dst": dst},
        )


async def test_rest_delete_of_a_reserved_path_answers_the_same_whether_it_exists(local_ws):
    ws, root = local_ws
    existing = await _delete_as_user(ws, _EXISTING)
    missing = await _delete_as_user(ws, _MISSING)
    assert existing.status_code == missing.status_code == 400, (existing.text, missing.text)
    assert (root / _EXISTING).exists()


async def test_rest_move_from_a_reserved_path_answers_the_same_whether_it_exists(local_ws):
    ws, root = local_ws
    existing = await _move_as_user(ws, _EXISTING, "out.jsonl")
    missing = await _move_as_user(ws, _MISSING, "out.jsonl")
    assert existing.status_code == missing.status_code == 400, (existing.text, missing.text)
    assert (root / _EXISTING).exists()
    assert not (root / "out.jsonl").exists()


async def test_the_delete_tool_answers_the_same_whether_a_reserved_path_exists(local_ws):
    import json

    from primer.toolset.workspaces import build_workspaces_toolset
    from tests._support.caller import caller
    from tests.tap.test_mcp_tap_tool import _Provider

    ws, root = local_ws

    class _Reg:
        async def get_workspace(self, workspace_id):
            return ws

    ts = build_workspaces_toolset(
        storage_provider=_Provider(), workspace_registry=_Reg(), tap_router=None,
    )
    outs = []
    for path in (_EXISTING, _MISSING):
        res = await ts.call(
            tool_name="delete_workspace_file",
            arguments={"workspace_id": "w", "path": path},
            principal=None, ctx=caller("user"),
        )
        assert res.is_error
        outs.append(json.loads(res.output)["type"])
    assert outs == ["bad-request", "bad-request"], outs
    assert (root / _EXISTING).exists()


async def test_a_local_write_into_a_reserved_dir_says_the_same_as_into_a_missing_one(local_ws):
    """LocalWorkspace.write_file checked is_dir before the reserved refusal:
    'is a directory' vs 'refusing to mutate' told a caller of the
    write_workspace_file tool that .state/sessions exists."""
    ws, _ = local_ws
    messages = []
    for path in (".state/sessions", ".state/nope"):
        with pytest.raises(BadRequestError) as info:
            await ws.write_file(path, b"x")
        messages.append(str(info.value).split(":")[0])
    assert messages[0] == messages[1], messages
    assert "reserved tree" in messages[0]
