"""Runtime state commits and single-shot ops (Part 3 Task 120: GI-5, GI-6, GI-10).

The handlers are called directly against a real git repo, and over the in-process server (``TestServer``) where the
connection matters.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from primer_runtime import ops
from tests.runtime.test_runtime_freezes import _TOKEN, Conn, runtime  # noqa: F401 - the in-process server fixture


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root / ".state"), *args], capture_output=True, text=True, check=True).stdout


def _files(root: Path, sha: str) -> list[str]:
    return sorted(_git(root, "show", "--name-only", "--format=", sha).split())


async def _landed(root: Path, subject: str, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        log = subprocess.run(["git", "-C", str(root / ".state"), "log", "--format=%s"], capture_output=True, text=True)
        if subject in log.stdout.split("\n"):
            return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Characterization: pinned on the unchanged code, unchanged after the fix
# ---------------------------------------------------------------------------


async def test_sequential_commits_hold_their_own_files_and_trailers(tmp_path: Path) -> None:
    root = str(tmp_path)
    a = await ops.state_commit({
        "files": {"sessions/a/1.json": _b64(b"a1")},
        "message": "a1\n\nX-Primer-Session: sa\nX-Primer-Op: turn\n",
    }, root)
    b = await ops.state_commit({
        "files": {"sessions/b/1.json": _b64(b"b1"), "sessions/b/2.json": _b64(b"b2")},
        "deletes": [],
        "message": "b1\n\nX-Primer-Session: sb\nX-Primer-Agent: ag\nX-Primer-Op: turn\n",
    }, root)
    c = await ops.state_commit({"files": {}, "deletes": ["sessions/a/1.json"], "message": "rm a1"}, root)

    assert set(a) == set(b) == set(c) == {"sha"}
    assert _files(tmp_path, a["sha"]) == ["sessions/a/1.json"]
    assert _files(tmp_path, b["sha"]) == ["sessions/b/1.json", "sessions/b/2.json"]
    assert _files(tmp_path, c["sha"]) == ["sessions/a/1.json"]
    assert _git(tmp_path, "rev-list", "HEAD").split() == [c["sha"], b["sha"], a["sha"]]
    assert not (tmp_path / ".state" / "sessions" / "a" / "1.json").exists()

    commits = (await ops.state_history({"limit": 10}, root))["commits"]
    assert [(x["sha"], x["subject"], x["session_id"], x["agent_id"], x["op"]) for x in commits] == [
        (c["sha"], "rm a1", None, None, None),
        (b["sha"], "b1", "sb", "ag", "turn"),
        (a["sha"], "a1", "sa", None, "turn"),
    ]


async def test_a_missing_repo_reads_null_fails_history_and_is_created_by_a_commit(tmp_path: Path) -> None:
    root = str(tmp_path)
    assert await ops.state_read({"paths": ["a.txt"]}, root) == {"files": {"a.txt": None}}
    try:
        await ops.state_history({"limit": 5}, root)
    except ops.OpError as exc:
        assert exc.code == "EINTERNAL"
        assert exc.message.startswith("git log --max-count=5 ")
    else:
        raise AssertionError("state_history on a missing repo did not fail")

    sha = (await ops.state_commit({"files": {"a.txt": _b64(b"x")}, "message": "first"}, root))["sha"]
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == sha
    assert await ops.state_read({"paths": ["a.txt", "b.txt"]}, root) == {"files": {"a.txt": _b64(b"x"), "b.txt": None}}


async def test_a_commit_with_nothing_to_commit_fails_internal(tmp_path: Path) -> None:
    root = str(tmp_path)
    await ops.state_commit({"files": {"a.txt": _b64(b"x")}, "message": "first"}, root)
    try:
        await ops.state_commit({"files": {"a.txt": _b64(b"x")}, "message": "again"}, root)
    except ops.OpError as exc:
        assert exc.code == "EINTERNAL"
        assert exc.message.startswith("git commit --quiet -m again exited 1")
    else:
        raise AssertionError("an empty commit did not fail")


async def test_state_ops_over_the_wire(runtime) -> None:  # noqa: F811
    connect, root, _url = runtime
    conn = await connect()
    rid = await conn.send("state_history", {"limit": 3})
    missing = await conn.reply(rid)
    assert missing["ok"] is False
    assert missing["error"]["code"] == "EINTERNAL"

    rid = await conn.send("state_commit", {"files": {"s/x.json": _b64(b"{}")}, "message": "x\n\nX-Primer-Session: s1\n"})
    sha = (await conn.reply(rid))["result"]["sha"]
    assert _files(root, sha) == ["s/x.json"]

    rid = await conn.send("state_read", {"paths": ["s/x.json", "s/none.json"]})
    assert (await conn.reply(rid))["result"] == {"files": {"s/x.json": _b64(b"{}"), "s/none.json": None}}

    rid = await conn.send("state_history", {"limit": 3, "session_id": "s1"})
    commits = (await conn.reply(rid))["result"]["commits"]
    assert [(x["sha"], x["session_id"]) for x in commits] == [(sha, "s1")]


async def test_a_commit_whose_client_left_still_lands(tmp_path: Path, monkeypatch) -> None:
    # Served as ``web.run_app`` serves it: ``TestServer`` cancels the handler when its client leaves, production does not.
    from primer_runtime.server import PROTOCOL_VERSION, build_app

    root = tmp_path / "ws"
    root.mkdir()
    runner = web.AppRunner(build_app(token=_TOKEN, workspace_root=str(root)))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    http = aiohttp.ClientSession()
    try:
        await _commit_then_leave(http, f"http://127.0.0.1:{port}/", root, PROTOCOL_VERSION, monkeypatch)
    finally:
        await http.close()
        await runner.cleanup()


async def _commit_then_leave(http, url, root: Path, protocol: str, monkeypatch) -> None:
    entered = asyncio.Event()
    gate = asyncio.Event()
    real_run_git = ops._run_git

    async def gated(state_dir, *args, **kwargs):
        if args[0] == "commit":
            entered.set()
            await gate.wait()
        return await real_run_git(state_dir, *args, **kwargs)

    monkeypatch.setattr(ops, "_run_git", gated)
    conn = Conn(await http.ws_connect(url, headers={"Authorization": f"Bearer {_TOKEN}"}))
    await conn.ws.send_json({"req_id": 0, "op": "hello", "args": {"protocol": protocol}})
    await conn.reply(0)
    await conn.send("state_commit", {"files": {"a.txt": _b64(b"a")}, "message": "left"})
    await asyncio.wait_for(entered.wait(), 10)
    conn.ws._response.connection.transport.abort()  # the client drops mid-commit
    await asyncio.sleep(0.3)  # the server sees the close and tears the connection down
    gate.set()
    assert await _landed(root, "left")
    assert _files(root, _git(root, "rev-parse", "HEAD").strip()) == ["a.txt"]


async def test_a_fresh_index_lock_fails_the_commit(tmp_path: Path) -> None:
    root = str(tmp_path)
    await ops.state_commit({"files": {"a.txt": _b64(b"1")}, "message": "first"}, root)
    (tmp_path / ".state" / ".git" / "index.lock").write_text("")  # a git that is still running holds it
    try:
        await ops.state_commit({"files": {"b.txt": _b64(b"2")}, "message": "second"}, root)
    except ops.OpError as exc:
        assert exc.code == "EINTERNAL"
        assert "index.lock" in exc.message
    else:
        raise AssertionError("a commit went through a live index.lock")
    assert (tmp_path / ".state" / ".git" / "index.lock").exists()


# ---------------------------------------------------------------------------
# GI-5: concurrent commits queue behind each other inside the runtime
# ---------------------------------------------------------------------------


def _anomalies(root: Path, i: int, results: list) -> list[str]:
    found = []
    for name, result in zip(("a", "b"), results):
        if isinstance(result, BaseException):
            found.append(f"{i}{name}: {type(result).__name__}: {getattr(result, 'message', result)}"[:200])
            continue
        other = "b" if name == "a" else "a"
        held = _files(root, result["sha"])
        if held != [f"sessions/{name}/{i}.json"]:
            found.append(f"{i}{name}: commit {result['sha'][:8]} holds {held} (other side: {other})")
    return found


async def test_concurrent_commits_each_hold_only_their_own_files(tmp_path: Path) -> None:
    root = str(tmp_path)
    await ops.state_commit({"files": {"init.txt": _b64(b"0")}, "message": "init"}, root)
    anomalies: list[str] = []
    for i in range(10):
        a = {"files": {f"sessions/a/{i}.json": _b64(b"a%d" % i)}, "message": f"a{i}"}
        b = {"files": {f"sessions/b/{i}.json": _b64(b"b%d" % i)}, "message": f"b{i}"}
        results = await asyncio.gather(ops.state_commit(a, root), ops.state_commit(b, root), return_exceptions=True)
        anomalies += _anomalies(tmp_path, i, results)
    assert anomalies == []


async def test_two_connections_committing_at_once_never_mix_or_fail(runtime) -> None:  # noqa: F811
    connect, root, _url = runtime
    first, second = await connect(), await connect()  # the API process and a worker each hold their own connection
    anomalies: list[str] = []
    for i in range(10):
        rid_a = await first.send("state_commit", {"files": {f"sessions/a/{i}.json": _b64(b"a")}, "message": f"a{i}"})
        rid_b = await second.send("state_commit", {"files": {f"sessions/b/{i}.json": _b64(b"b")}, "message": f"b{i}"})
        replies = [await first.reply(rid_a), await second.reply(rid_b)]
        results = [r["result"] if r["ok"] else RuntimeError(r["error"]["message"]) for r in replies]
        anomalies += _anomalies(root, i, results)
    assert anomalies == []
