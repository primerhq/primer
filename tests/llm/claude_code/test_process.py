"""The generic agent-runtime child layer: scrubbed env, temp-dir lifecycle, group kill, orphan sweep."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from primer.common.process_group import NEW_SESSION, kill_process_group
from primer.llm.claude_code.process import (
    ALLOWED,
    _ChildRecord,
    _child_pgid,
    child_env,
    child_process,
    read_stderr_tail,
    sweep_orphans,
)

_TMP_PREFIX = f"primer-cc-{os.getpid()}-"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _my_tmp_dir() -> Path:
    matches = [p for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith(_TMP_PREFIX)]
    assert len(matches) == 1, f"expected exactly one live {_TMP_PREFIX!r} dir, found {matches}"
    return matches[0]


async def _dead_pid() -> int:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()
    return proc.pid


def test_child_env_is_minimal(monkeypatch):
    monkeypatch.setenv("PRIMER_DB__PASSWORD", "x")
    monkeypatch.setenv("HTTPS_PROXY", "http://p")
    env = child_env(["HTTPS_PROXY"])
    assert "PRIMER_DB__PASSWORD" not in env
    assert env["HTTPS_PROXY"] == "http://p"
    assert env["CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"] == "0"
    assert set(env) <= ALLOWED | {"HTTPS_PROXY", "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"}


def test_child_env_drops_unset_names():
    env = child_env(["SURELY_UNSET_ENV_NAME_12345"])
    assert "SURELY_UNSET_ENV_NAME_12345" not in env
    assert env["CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"] == "0"


async def test_child_is_killed_and_dir_removed_on_exit():
    async with child_process(lambda d: ["sleep", "60"], env=child_env([]), system_text="s") as proc:
        tmp = _my_tmp_dir()
        pid = proc.pid
        assert (tmp / "system.md").read_text(encoding="utf-8") == "s"
        assert json.loads((tmp / "child.json").read_text(encoding="utf-8")) == {"pid": pid, "pgid": pid}
        assert tmp.stat().st_mode & 0o777 == 0o700
        assert os.getpgid(pid) == pid, "child must lead its own process group"
    assert not tmp.exists()
    assert not _alive(pid)


async def test_child_is_killed_on_cancel():
    spawned: dict[str, asyncio.subprocess.Process] = {}

    async def hold() -> None:
        async with child_process(lambda d: ["sleep", "60"], env=child_env([]), system_text="s") as proc:
            spawned["proc"] = proc
            await asyncio.sleep(60)

    task = asyncio.create_task(hold())
    for _ in range(200):
        if "proc" in spawned:
            break
        await asyncio.sleep(0.01)
    assert "proc" in spawned, "child never spawned"
    tmp = _my_tmp_dir()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not tmp.exists()
    assert not _alive(spawned["proc"].pid)


async def test_sweep_kills_orphans_of_dead_workers(tmp_path: Path):
    dead = await _dead_pid()
    sleeper = await asyncio.create_subprocess_exec("sleep", "60", **NEW_SESSION)
    try:
        orphan_dir = tmp_path / f"primer-cc-{dead}-x"
        orphan_dir.mkdir(mode=0o700)
        (orphan_dir / "child.json").write_text(
            json.dumps({"pid": sleeper.pid, "pgid": sleeper.pid}), encoding="utf-8"
        )
        assert await sweep_orphans(tmp_path) == 1
        assert not orphan_dir.exists()
        await asyncio.wait_for(sleeper.wait(), timeout=2)
    finally:
        await kill_process_group(sleeper)


async def test_sweep_leaves_live_workers_alone(tmp_path: Path):
    live_dir = tmp_path / f"primer-cc-{os.getpid()}-keep"
    live_dir.mkdir(mode=0o700)
    (live_dir / "child.json").write_text(
        json.dumps({"pid": os.getpid(), "pgid": os.getpid()}), encoding="utf-8"
    )
    assert await sweep_orphans(tmp_path) == 0
    assert live_dir.exists()


async def test_sweep_ignores_non_primer_dirs(tmp_path: Path):
    dead = await _dead_pid()
    stray = tmp_path / f"foo-bar-{dead}"
    stray.mkdir()
    assert await sweep_orphans(tmp_path) == 0
    assert stray.exists()


async def test_sweep_deletes_a_trusted_dir_without_a_child_json(tmp_path: Path):
    """A trusted dir whose ``child.json`` never exists (a crash between mkdtemp and the
    write) is the NORMAL case: it is rmtree'd WITHOUT a kill and counted as cleaned."""
    dead = await _dead_pid()
    d = tmp_path / f"primer-cc-{dead}-x"
    d.mkdir(mode=0o700)
    assert await sweep_orphans(tmp_path) == 1
    assert not d.exists()


async def test_sweep_skips_a_trusted_dir_with_a_symlinked_child_json(tmp_path: Path):
    """A symlinked ``child.json`` inside a trusted dir is a real tampering signal: the
    dir is neither killed NOR deleted (and the target's group is not signalled)."""
    dead = await _dead_pid()
    d = tmp_path / f"primer-cc-{dead}-symjson"
    d.mkdir(mode=0o700)
    target = tmp_path / "outside-child.json"
    target.write_text(
        json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}), encoding="utf-8"
    )
    (d / "child.json").symlink_to(target)
    assert await sweep_orphans(tmp_path) == 0
    assert d.exists(), "a tampered record leaves the directory alone"
    assert target.exists()


async def test_sweep_skips_a_planted_dir_with_the_wrong_mode(tmp_path: Path):
    """Anyone who can write to the shared temp root could plant a ``primer-cc-<deadpid>-*`` name: a directory that is
    not a 0700 directory of this uid is neither killed nor deleted, and its recorded group is not signalled."""
    dead = await _dead_pid()
    sleeper = await asyncio.create_subprocess_exec("sleep", "60", **NEW_SESSION)
    try:
        planted = tmp_path / f"primer-cc-{dead}-planted"
        # chmod AFTER mkdir: mkdir's mode argument is masked by umask, so an
        # explicit chmod is the only reliable way to land exactly on 0755.
        planted.mkdir()
        os.chmod(planted, 0o755)
        (planted / "child.json").write_text(
            json.dumps({"pid": sleeper.pid, "pgid": sleeper.pid}), encoding="utf-8"
        )
        assert await sweep_orphans(tmp_path) == 0
        assert planted.exists(), "an untrusted directory is neither killed nor deleted"
        assert _alive(sleeper.pid), "the group of an untrusted directory is not signalled"
    finally:
        await kill_process_group(sleeper)


async def test_sweep_skips_a_symlinked_dir(tmp_path: Path):
    dead = await _dead_pid()
    sleeper = await asyncio.create_subprocess_exec("sleep", "60", **NEW_SESSION)
    try:
        real = tmp_path / "real-target"
        real.mkdir(mode=0o700)
        (real / "child.json").write_text(
            json.dumps({"pid": sleeper.pid, "pgid": sleeper.pid}), encoding="utf-8"
        )
        link = tmp_path / f"primer-cc-{dead}-link"
        link.symlink_to(real)
        assert await sweep_orphans(tmp_path) == 0
        assert link.exists(), "a symlink is not a directory the sweep trusts (lstat)"
        assert real.exists()
        assert _alive(sleeper.pid), "a symlinked directory's group is not signalled"
    finally:
        await kill_process_group(sleeper)


async def test_sweep_never_kills_the_own_process_group(tmp_path: Path):
    dead = await _dead_pid()
    d = tmp_path / f"primer-cc-{dead}-own"
    d.mkdir(mode=0o700)
    (d / "child.json").write_text(
        json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}), encoding="utf-8"
    )
    assert await sweep_orphans(tmp_path) == 0
    assert d.exists(), "a record pointing at our own group is planted or corrupted, not trusted"


def test_child_pgid_tri_state(tmp_path: Path):
    """The record is a tri-state: a killable pgid, nothing-to-kill (no record, or a
    dangerous pgid: the dir may be deleted, no group is signalled), or a TAMPERED
    record (symlink, own group: the dir must be left alone)."""
    rec = _child_pgid(tmp_path / "missing.json")
    assert rec.pgid is None and not rec.tampered, "no record: nothing to kill"
    for pgid in (0, 1, -5):
        f = tmp_path / f"c{pgid}.json"
        f.write_text(json.dumps({"pgid": pgid}), encoding="utf-8")
        assert _child_pgid(f).pgid is None and not _child_pgid(f).tampered
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"pid": 99, "pgid": 4242}), encoding="utf-8")
    assert _child_pgid(ok) == _ChildRecord(pgid=4242, tampered=False)
    own = tmp_path / "own.json"
    own.write_text(json.dumps({"pgid": os.getpgrp()}), encoding="utf-8")
    assert _child_pgid(own).tampered, "a record pointing at our own group is a tampering signal"
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"pgid": 4242}), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert _child_pgid(link).tampered, "a symlinked record is a tampering signal"
    inf = tmp_path / "inf.json"
    inf.write_text(json.dumps({"pgid": 1e999}), encoding="utf-8")  # int(inf) raises OverflowError
    assert _child_pgid(inf).pgid is None and not _child_pgid(inf).tampered, "a non-integer pgid is not killable"


async def test_child_runs_in_the_temp_dir():
    code = "import os; print(os.getcwd())"
    async with child_process(lambda d: [sys.executable, "-c", code], env=child_env([]), system_text="s") as proc:
        tmp = _my_tmp_dir()
        out = await asyncio.wait_for(proc.stdout.read(), timeout=10)
    # .resolve(): the temp dir may sit under a symlinked root (macOS /var -> /private/var)
    assert out.decode("utf-8").strip() == str(tmp.resolve())


async def test_spawn_failure_leaves_no_dir():
    with pytest.raises(FileNotFoundError):
        async with child_process(
            lambda d: ["/definitely/not/a/binary"], env=child_env([]), system_text="s"
        ):
            pass
    leftovers = [p for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith(_TMP_PREFIX)]
    assert not leftovers


async def test_chatty_stderr_does_not_block():
    code = "import sys; sys.stderr.write('x' * 200000 + 'boom'); sys.stdout.write('ok')"
    async with child_process(lambda d: [sys.executable, "-c", code], env=child_env([]), system_text="s") as proc:
        tmp = _my_tmp_dir()
        out = await asyncio.wait_for(proc.stdout.read(), timeout=10)
        assert out == b"ok"
        tail = read_stderr_tail(tmp)
    assert tail.endswith("boom")
    assert len(tail) <= 4000
