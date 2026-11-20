"""Real temporary Git repos and in-process WebSockets; no container or user data."""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import subprocess
import sys

import pytest
from aiohttp.test_utils import TestServer
from primer_runtime import state_inspect
from primer_runtime.ops import OpError, state_commit, state_history
from primer_runtime.protocol import ErrorCode
from primer_runtime.server import build_app
from primer.workspace.runtime.runtime_client import RuntimeClient
from primer.workspace.runtime.ws_sandbox import WSSandbox
from primer.workspace.sandbox.state import SandboxStateRepo


async def commit(root, files, message="change", deletes=()):
    result = await state_commit({
        "files": {p: base64.b64encode(b).decode() for p, b in files.items()},
        "deletes": list(deletes), "message": message, "allow_empty": True,
    }, str(root))
    return result["sha"]


async def test_root_modify_delete_binary_and_odd_paths(tmp_path):
    odd = 'space " tab\t newline\nname.txt'
    first = await commit(tmp_path, {odd: b"one\n", "binary": b"\0\xff", "gone": b"bye\n"}, "first\n\nbody")
    root = await state_inspect.state_show_commit({"sha": first}, str(tmp_path))
    assert root["parent"] is None and root["subject"] == "first" and root["body"] == "body"
    assert {f["path"] for f in root["files"]} == {odd, "binary", "gone"}
    assert "+one" in next(f["patch"] for f in root["files"] if f["path"] == odd)
    assert "Binary files" in next(f["patch"] for f in root["files"] if f["path"] == "binary")
    second = await commit(tmp_path, {odd: b"two\nextra\n"}, deletes=["gone"])
    detail = await state_inspect.state_show_commit({"sha": second[:7]}, str(tmp_path))
    assert detail["sha"] == second[:7] and detail["parent"] == first
    assert {f["path"]: f["status"] for f in detail["files"]} == {odd: "M", "gone": "D"}
    assert "-one" in next(f["patch"] for f in detail["files"] if f["path"] == odd)
    history = (await state_history({"with_files": True}, str(tmp_path)))["commits"]
    assert [r["sha"] for r in history] == [second, first]
    delta = next(f for f in history[0]["files"] if f["path"] == odd)
    assert delta == {"path": odd, "additions": 2, "deletions": 1, "binary": False}
    assert next(f for f in history[1]["files"] if f["path"] == "binary")["binary"] is True
    assert "files" not in (await state_history({}, str(tmp_path)))["commits"][0]


@pytest.mark.parametrize("sha", ["--output=pwned", "HEAD", "abc", "x" * 40, "a" * 65, "aaaa:file", None])
async def test_hostile_sha_never_invokes_git(tmp_path, monkeypatch, sha):
    async def forbidden(*args):
        pytest.fail("invalid revision reached Git")
    monkeypatch.setattr(state_inspect, "_git", forbidden)
    with pytest.raises(OpError) as raised:
        await state_inspect.state_show_commit({"sha": sha}, str(tmp_path))
    assert raised.value.code == ErrorCode.EPROTOCOL
    assert list(tmp_path.iterdir()) == []


async def test_unknown_commit_missing_repo_and_blob_are_not_found(tmp_path):
    with pytest.raises(OpError) as missing:
        await state_inspect.state_show_commit({"sha": "0" * 40}, str(tmp_path))
    assert missing.value.code == ErrorCode.ENOENT
    await commit(tmp_path, {"file": b"hi"})
    blob = subprocess.check_output(["git", "-C", str(tmp_path / ".state"), "rev-parse", "HEAD:file"]).decode().strip()
    for sha in ("0" * 40, blob):
        with pytest.raises(OpError) as raised:
            await state_inspect.state_show_commit({"sha": sha}, str(tmp_path))
        assert raised.value.code == ErrorCode.ENOENT


@pytest.mark.parametrize("link_git", [False, True])
async def test_symlink_repo_escape_is_refused(tmp_path, link_git):
    outside = tmp_path / "outside"
    outside.mkdir()
    sha = await commit(outside, {"secret": b"outside"})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if link_git:
        (workspace / ".state").mkdir()
        (workspace / ".state" / ".git").symlink_to(outside / ".state" / ".git", target_is_directory=True)
    else:
        (workspace / ".state").symlink_to(outside / ".state", target_is_directory=True)
    with pytest.raises(OpError) as raised:
        await state_inspect.state_show_commit({"sha": sha}, str(workspace))
    assert raised.value.code == ErrorCode.EACCES


async def test_history_filters_are_literal_and_empty_commits_keep_empty_file_list(tmp_path):
    await commit(tmp_path, {"a": b"a"}, "one\n\nX-Primer-Session: s1\nX-Primer-Agent: ag.1")
    last = await commit(tmp_path, {}, "two\n\nX-Primer-Session: s2\nX-Primer-Agent: ag2")
    assert (await state_history({"with_files": True, "session_id": "s.*"}, str(tmp_path)))["commits"] == []
    rows = (await state_history({"with_files": True, "session_id": "s1", "agent_id": "ag.1"}, str(tmp_path)))["commits"]
    assert len(rows) == 1 and rows[0]["subject"] == "one"
    assert rows[0]["agent_id"] == "ag.1"
    rows = (await state_history({"with_files": True, "limit": 1}, str(tmp_path)))["commits"]
    assert len(rows) == 1 and rows[0]["sha"] == last and rows[0]["files"] == []


async def test_oversized_diff_fails_explicitly_not_as_partial_success(tmp_path, monkeypatch):
    sha = await commit(tmp_path, {"big": b"a long line\n" * 1000})
    real = asyncio.create_subprocess_exec
    processes = []
    async def capture(*args, **kwargs):
        proc = await real(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setattr(state_inspect, "MAX_INSPECT_BYTES", 512)
    with pytest.raises(OpError, match="output limit"):
        await state_inspect.state_show_commit({"sha": sha}, str(tmp_path))
    assert processes and all(p.returncode is not None for p in processes)


@pytest.mark.parametrize("cancel", [False, True])
async def test_inspection_timeout_or_cancel_reaps_subprocess(tmp_path, monkeypatch, cancel):
    from primer_runtime import ops
    real = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()
    async def slow(*args, **kwargs):
        proc = await real(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(proc)
        started.set()
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow)
    monkeypatch.setattr(ops, "_subprocess_timeout", lambda: 10 if cancel else 0.02)
    task = asyncio.create_task(state_inspect._git(tmp_path, "show"))
    await started.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(OpError) as raised:
            await task
        assert raised.value.code == ErrorCode.ETIMEDOUT
    assert processes[0].returncode is not None


async def test_runtime_to_sandbox_roundtrip_over_real_websocket(tmp_path):
    async with TestServer(build_app(token="test", workspace_root=str(tmp_path))) as server:
        client = RuntimeClient(url=str(server.make_url("/")).replace("http:", "ws:"), token="test")
        await client.connect()
        try:
            sandbox = WSSandbox(runtime_client=client, container_id="isolated", workspace_root=str(tmp_path))
            repo = SandboxStateRepo(sandbox, state_path=str(tmp_path / ".state"), workspace_id="w")
            sha = await client.state_commit(files={"session.json": b"one\n"}, deletes=[], message="seed")
            detail = await repo.show_commit(sha)
            assert detail["files"][0]["path"] == "session.json" and "+one" in detail["files"][0]["patch"]
            rows = await repo.history(with_files=True)
            assert rows[0].files[0].additions == 1
            with pytest.raises(FileNotFoundError):
                await repo.show_commit("0" * 40)
        finally:
            await client.aclose()


async def test_rename_is_unambiguous_and_merge_diff_uses_first_parent(tmp_path):
    await commit(tmp_path, {"old": b"content\n"}, "base")
    renamed = await commit(tmp_path, {"new": b"content\n"}, "rename", deletes=["old"])
    shown = await state_inspect.state_show_commit({"sha": renamed}, str(tmp_path))
    assert {f["path"]: f["status"] for f in shown["files"]} == {"old": "D", "new": "A"}
    state = tmp_path / ".state"
    def git(*args):
        return subprocess.check_output(["git", "-C", str(state), *args]).decode().strip()
    git("checkout", "-b", "side")
    await commit(tmp_path, {"side": b"side\n"}, "side")
    git("checkout", "main")
    parent = await commit(tmp_path, {"main": b"main\n"}, "main")
    git("merge", "--no-ff", "side", "-m", "merge")
    shown = await state_inspect.state_show_commit({"sha": git("rev-parse", "HEAD")}, str(tmp_path))
    assert shown["parent"] == parent
    assert [(f["path"], f["status"]) for f in shown["files"]] == [("side", "A")]
    history = (await state_history({"with_files": True, "limit": 1}, str(tmp_path)))["commits"]
    assert [f["path"] for f in history[0]["files"]] == ["side"]


async def test_read_does_not_invoke_diff_drivers_or_inherit_git_redirection(tmp_path, monkeypatch):
    state = tmp_path / ".state"
    first = await commit(tmp_path, {"file.txt": b"one\n", ".gitattributes": b"*.txt diff=custom\n"})
    sha = await commit(tmp_path, {"file.txt": b"two\n"})
    marker = tmp_path / "external-ran"
    driver = tmp_path / "driver"
    driver.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    driver.chmod(0o700)
    for key in ("diff.external", "diff.custom.textconv"):
        subprocess.run(["git", "-C", str(state), "config", key, str(driver)], check=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(driver))
    shown = await state_inspect.state_show_commit({"sha": sha}, str(tmp_path))
    assert shown["parent"] == first and "+two" in shown["files"][0]["patch"]
    await state_history({"with_files": True}, str(tmp_path))
    assert not marker.exists()


async def test_json_expansion_is_bounded_before_websocket_framing(tmp_path, monkeypatch):
    from primer_runtime.protocol import Response, serialize
    from primer_runtime.server import _MAX_MSG_SIZE
    from primer.workspace.runtime.runtime_client import _MAX_WS_MSG_SIZE

    assert state_inspect.MAX_RESULT_BYTES + 8192 < min(_MAX_MSG_SIZE, _MAX_WS_MSG_SIZE)
    sha = await commit(tmp_path, {"control.txt": b"\x01" * 1000 + b"\n"})
    result = await state_inspect.state_show_commit({"sha": sha}, str(tmp_path))
    frame = serialize(Response(req_id=1, ok=True, result=result)).encode()
    assert len(frame) < state_inspect.MAX_RESULT_BYTES
    assert len(frame) > 6000  # control characters expand to six-byte JSON escapes
    monkeypatch.setattr(state_inspect, "MAX_RESULT_BYTES", 4000)
    with pytest.raises(OpError, match="JSON response limit"):
        await state_inspect.state_show_commit({"sha": sha}, str(tmp_path))
