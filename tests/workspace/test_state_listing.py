"""Every production state repo lists a directory (``ListingStateRepo``), so a segmented session log is read whole and a gap is seen, never probed past
(segmented session log, design D5 and section 5)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from primer.int.sandbox import FileStat
from primer.int.state_repo import ListingStateRepo
from primer.workspace.local.state import LocalStateRepo
from primer.workspace.sandbox.state import SandboxStateRepo
from primer.workspace.segments import LOG_NAME, SegmentGap, SessionLog, StateRepoSource

_LOG = {LOG_NAME: b'{"role": "user"}\n', "messages.0001.jsonl": b'{"role": "assistant"}\n', "messages.0002.jsonl": b'{"seq": 1, "kind": "done"}\n'}
_SLOT = "sessions/s1"
_STATE = "/workspace/.state"


class _SandboxDouble:
    """The runtime ops the sandbox state repo uses (``state_read``) and the sandbox's own ``stat`` and ``list_dir`` (absolute paths, as the runtime answers)."""

    protocol_version = "1.1"

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = {f"{_STATE}/{rel}": data for rel, data in files.items()}
        self.listed: list[str] = []

    async def state_commit(self, **_kw) -> str:
        raise AssertionError("read-only")

    async def state_history(self, **_kw) -> list[dict]:
        return []

    async def state_read(self, paths: list[str]) -> dict[str, bytes | None]:
        return {p: self.files.get(f"{_STATE}/{p}") for p in paths}

    async def stat(self, path: str):
        if path in self.files or any(f.startswith(path + "/") for f in self.files):
            return SimpleNamespace(kind="dir" if path not in self.files else "file")
        return None

    async def list_dir(self, path: str) -> list[FileStat]:
        self.listed.append(path)
        now = datetime.now(timezone.utc)
        return [
            FileStat(path=name, kind="file", size_bytes=len(data), mode=0o644, modified_at=now)
            for name, data in ((f.rsplit("/", 1)[-1], d) for f, d in self.files.items() if f.rsplit("/", 1)[0] == path)
        ]


def _local(tmp_path, files: dict[str, bytes]) -> LocalStateRepo:
    slot = tmp_path / ".state" / _SLOT
    slot.mkdir(parents=True)
    for name, data in files.items():
        (slot / name).write_bytes(data)
    (slot / "session.json").write_text("{}")
    (slot / "nested").mkdir()
    return LocalStateRepo(tmp_path / ".state", workspace_id="w")


def _sandbox(files: dict[str, bytes]) -> SandboxStateRepo:
    files = {**files, "session.json": b"{}"}
    return SandboxStateRepo(_SandboxDouble({f"{_SLOT}/{n}": d for n, d in files.items()}), state_path=_STATE, workspace_id="w")


@pytest.fixture(params=["local", "sandbox"])
def make_repo(request, tmp_path):
    return (lambda files: _local(tmp_path, files)) if request.param == "local" else _sandbox


def test_both_production_repos_are_listing_repos(make_repo):
    assert isinstance(make_repo(_LOG), ListingStateRepo)


async def test_a_repo_lists_the_files_of_a_directory_and_an_absent_directory_lists_nothing(make_repo):
    repo = make_repo(_LOG)
    assert sorted(await repo.list_state_dir(_SLOT)) == sorted([*_LOG, "session.json"]), "files only: the nested directory is not a name"
    assert await repo.list_state_dir("sessions/none") == []


async def test_the_whole_log_is_read_through_the_listing(make_repo):
    log = SessionLog(StateRepoSource(make_repo(_LOG)), _SLOT)
    assert await log.read_all() == b"".join(_LOG.values())
    assert [s.name for s in await log.segments()] == list(_LOG)


async def test_a_gap_is_seen_through_the_repo_not_probed_past(make_repo):
    gapped = {k: v for k, v in _LOG.items() if k != "messages.0001.jsonl"}
    log = SessionLog(StateRepoSource(make_repo(gapped)), _SLOT)
    with pytest.raises(SegmentGap):
        await log.read_all()


async def test_a_session_with_no_log_is_empty(make_repo):
    log = SessionLog(StateRepoSource(make_repo({})), _SLOT)
    assert await log.segments() == [] and await log.read_all() == b""
