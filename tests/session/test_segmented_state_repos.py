"""Seglog T7: the segment-aware readers of a state repo, on the local repo and on the sandbox repo, equal the single-file pins (see ``tests/_support/segmented_log.py``).

The matrix installs each log, cut by each plan, in a real local workspace. The sandbox repo is the real :class:`SandboxStateRepo` over a protocol double that serves
the same ``.state`` directory through the runtime's own ops (``state_read``, ``stat``, ``list_dir``), so the listing path of a sandbox is exercised without a
container. What is compared is what the facade's readers derive (the history from the newest compaction marker on, the compaction state, the highest event seq and the
whole log's bytes); the readers that still open ``messages.jsonl`` alone are the matrix's ``KNOWN_SEGMENT_GAPS`` and are not touched here.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.int.sandbox import FileStat
from primer.workspace.sandbox.state import SandboxStateRepo
from primer.workspace.segments import SessionLog, StateRepoSource
from primer.workspace.session import last_compaction_state, reconstruct_compacted_history
from tests._support.segmented_log import PLANS, _msgs, _norm, install, open_env, scenario_logs, split_log

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")

PINS = Path(__file__).parent / "fixtures" / "segmented_readers"
LOGS = scenario_logs()
_STATE = "/workspace/.state"
_FACADE_READERS = ("history", "compaction_state", "max_event_seq")


class _RuntimeDouble:
    """The runtime ops the sandbox state repo uses, answered from a host directory (``/workspace/.state/<rel>`` is ``root/<rel>``)."""

    protocol_version = "1.1"

    def __init__(self, root: Path) -> None:
        self._root = root

    def _host(self, path: str) -> Path:
        assert path == _STATE or path.startswith(_STATE + "/"), path
        return self._root / path[len(_STATE) :].lstrip("/")

    async def state_commit(self, **_kw) -> str:
        raise AssertionError("read-only")

    async def state_history(self, **_kw) -> list[dict]:
        return []

    async def state_read(self, paths: list[str]) -> dict[str, bytes | None]:
        return {p: (self._host(f"{_STATE}/{p}").read_bytes() if self._host(f"{_STATE}/{p}").is_file() else None) for p in paths}

    async def stat(self, path: str):
        host = self._host(path)
        return None if not host.exists() else type("S", (), {"kind": "dir" if host.is_dir() else "file"})()

    async def list_dir(self, path: str) -> list[FileStat]:
        now = datetime.now(timezone.utc)
        return [
            FileStat(path=f"{path}/{e.name}", kind="dir" if e.is_dir() else "file", size_bytes=e.stat().st_size, mode=0o644, modified_at=now)
            for e in sorted(self._host(path).iterdir())
        ]


async def _facade_answers(repo, sid: str) -> dict:
    log = SessionLog(StateRepoSource(repo), f"sessions/{sid}")
    _, data = await log.since_last_marker()
    lines = data.decode("utf-8", errors="replace").splitlines()
    got = {
        "history": _msgs(reconstruct_compacted_history(lines), sid),
        "compaction_state": list(last_compaction_state(lines)),
        "max_event_seq": await log.max_event_seq(),
    }
    return {k: _norm(v, sid) for k, v in got.items()}


@pytest.mark.parametrize("plan", sorted(PLANS))
@pytest.mark.parametrize("name", sorted(LOGS))
async def test_local_and_sandbox_repos_read_every_segmented_log_like_the_single_file(name, plan, tmp_path):
    env = await open_env(tmp_path)
    try:
        parts = split_log(LOGS[name], PLANS[plan](LOGS[name]))
        install(env, parts)
        want = json.loads((PINS / f"{name}.json").read_text())
        sandbox = SandboxStateRepo(_RuntimeDouble(env.workspace.root / env.state_path), state_path=_STATE, workspace_id="w")
        for backend, repo in (("local", env.session._state), ("sandbox", sandbox)):
            got = await _facade_answers(repo, env.sid)
            assert got == {r: want[r] for r in _FACADE_READERS}, (backend, [r for r in _FACADE_READERS if got[r] != want[r]])
            log = SessionLog(StateRepoSource(repo), f"sessions/{env.sid}")
            assert [s.name for s in await log.segments()][:1] == ["messages.jsonl"] and len(await log.segments()) == len(parts), backend
            assert await log.read_all() == b"".join(p.replace(b"sess-fixed", env.sid.encode()) for p in parts), backend
    finally:
        await env.close()
