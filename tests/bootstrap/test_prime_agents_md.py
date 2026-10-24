"""Prime's agents.md is seeded when absent, and never overwritten or shadowed by a boot (pivot plan P2, task 9).

The platform ships ``primer/bootstrap/prime_agents.md``. The ensure pass (every boot, and ``POST /v1/setup/seed``) writes it to the
default workspace's root as ``agents.md`` only when neither ``agents.md`` nor ``AGENTS.md`` is there: a person may have edited the
file, or keep their instructions in ``AGENTS.md`` (which the loader reads only when ``agents.md`` is absent, so writing ``agents.md``
beside it would shadow it). Only the explicit reset (task 10) rewrites it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone

import pytest

from primer.bootstrap import prime as prime_module
from primer.bootstrap.prime import (
    PRIME_AGENTS_MD,
    ensure_prime_agents_md,
    reset_prime_agents_md,
    shipped_prime_agents_md,
)
from primer.bootstrap.seed import run_ensure_pass
from primer.model.except_ import NotFoundError, WorkspaceUnreachableError
from primer.model.workspace import FileEntry, Workspace, WorkspaceRuntimeMeta
from primer.workspace.instructions import AGENTS_MD_NAMES, load_workspace_instructions


def _row(workspace_id: str) -> Workspace:
    return Workspace(
        id=workspace_id,
        name="prime",
        template_id="local-default",
        provider_id="local",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
    )


class _Ws:
    """A live workspace handle: ``file_info`` answers from ``files`` (NotFoundError when absent), ``write_file`` records."""

    def __init__(self, files: dict[str, bytes] | None = None, *, dirs: set[str] | None = None) -> None:
        self.id = "prime"
        self.files: dict[str, bytes] = dict(files or {})
        self.dirs: set[str] = set(dirs or ())
        self.writes: list[str] = []
        self.checked: list[str] = []

    async def file_info(self, path: str) -> FileEntry:
        self.checked.append(path)
        if path in self.dirs:
            return FileEntry(path=path, kind="dir", size_bytes=0, modified_at=datetime.now(timezone.utc))
        if path not in self.files:
            raise NotFoundError(f"{path!r} not found")
        return FileEntry(path=path, kind="file", size_bytes=len(self.files[path]), modified_at=datetime.now(timezone.utc))

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise NotFoundError(f"{path!r} not found")
        return self.files[path]

    async def write_file(self, path: str, content: bytes) -> None:
        self.writes.append(path)
        self.files[path] = content


class _Registry:
    def __init__(self, ws: _Ws) -> None:
        self.ws = ws
        self.asked: list[str] = []

    async def get_workspace(self, workspace_id: str) -> _Ws:
        self.asked.append(workspace_id)
        return self.ws


async def _prime(sp, workspace_id: str = "prime") -> None:
    await sp.get_storage(Workspace).create(_row(workspace_id))
    await sp.set_default_workspace_id(workspace_id)


# ---- the shipped file --------------------------------------------------------------------------------------------------------------


def test_the_shipped_file_is_packaged_utf8_and_has_no_em_dash():
    text = shipped_prime_agents_md().decode("utf-8")
    assert text.strip() and chr(0x2014) not in text
    assert text.startswith("# prime\n")


#: The ledger line, exactly (review of #887, item 3): the explorer's and the planner's prompts say they never write files, so the ledger
#: is the operator's, and the line says so instead of telling every agent in prime to keep one.
LEDGER_LINE = (
    "The operator keeps one ledger file per task under `tasks/` in this workspace, with the goal, where the work runs and what has "
    "happened so far. An agent whose own role says it never writes files leaves the ledger to the operator."
)


def test_the_ledger_is_the_operator_s():
    lines = shipped_prime_agents_md().decode("utf-8").splitlines()
    assert LEDGER_LINE in lines
    assert [line for line in lines if "ledger" in line] == [LEDGER_LINE]


def test_the_shipped_file_names_no_tool_id():
    """The interim text is plain guidance: a tool id in it would go stale with the tool (P4 writes the final text)."""
    text = shipped_prime_agents_md().decode("utf-8")
    assert not re.search(r"\b[a-z_]+(::|__)[a-z_]+", text), text


def test_the_file_is_written_under_the_name_the_loader_reads_first():
    assert PRIME_AGENTS_MD == "agents.md" == AGENTS_MD_NAMES[0]


# ---- seeding ----------------------------------------------------------------------------------------------------------------------


async def test_writes_the_shipped_file_when_absent(fake_storage_provider):
    await _prime(fake_storage_provider)
    ws = _Ws()
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws)) is True
    assert ws.files == {"agents.md": shipped_prime_agents_md()}


async def test_an_edited_file_is_never_overwritten(fake_storage_provider):
    await _prime(fake_storage_provider)
    ws = _Ws({"agents.md": b"mine"})
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws)) is False
    assert ws.files == {"agents.md": b"mine"} and ws.writes == []


async def test_an_empty_file_is_never_overwritten(fake_storage_provider):
    """Emptying the file is how a person runs prime without instructions: an empty file adds nothing to a prompt, and it is not absent."""
    await _prime(fake_storage_provider)
    ws = _Ws({"agents.md": b""})
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws)) is False
    assert ws.files == {"agents.md": b""} and ws.writes == []


async def test_an_upper_case_AGENTS_md_is_not_shadowed(fake_storage_provider):
    await _prime(fake_storage_provider)
    ws = _Ws({"AGENTS.md": b"MINE"})
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws)) is False
    assert "agents.md" not in ws.files and ws.writes == []
    assert await load_workspace_instructions(ws) == "MINE"


async def test_a_directory_called_agents_md_is_left_alone(fake_storage_provider):
    """Any entry under either name counts as present: the boot never writes over, or beside, something a person put there."""
    await _prime(fake_storage_provider)
    ws = _Ws(dirs={"agents.md"})
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws)) is False
    assert ws.writes == []


async def test_no_default_workspace_is_a_no_op(fake_storage_provider):
    ws = _Ws()
    registry = _Registry(ws)
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=registry) is False
    assert registry.asked == [] and ws.writes == []


async def test_no_registry_is_a_no_op(fake_storage_provider):
    await _prime(fake_storage_provider)
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=None) is False


async def test_a_recorded_id_without_its_row_is_a_no_op(fake_storage_provider):
    """The default-workspace step recorded the id and then failed to create the row: nothing to write into yet, and nothing is
    asked of the backend. The next pass, which creates the row, seeds the file."""
    await fake_storage_provider.set_default_workspace_id("prime")
    ws = _Ws()
    registry = _Registry(ws)
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=registry) is False
    assert registry.asked == [] and ws.writes == []


async def test_the_legacy_default_workspace_gets_the_file_under_its_own_id(fake_storage_provider):
    """An upgraded install's default keeps the id ``primer``: the file goes to that workspace, never to a new ``prime``."""
    await _prime(fake_storage_provider, "primer")
    ws = _Ws()
    registry = _Registry(ws)
    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=registry) is True
    assert registry.asked == ["primer"] and ws.files == {"agents.md": shipped_prime_agents_md()}


async def test_a_workspace_the_backend_cannot_find_is_a_no_op(fake_storage_provider):
    await _prime(fake_storage_provider)

    class _Gone:
        async def get_workspace(self, workspace_id):
            raise NotFoundError(f"Workspace {workspace_id!r} row exists but the backend has no live instance")

    assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Gone()) is False


async def test_a_check_that_fails_writes_nothing_and_raises(fake_storage_provider):
    """A failure that is not "absent" never reads as absent: the step records it and the next boot tries again."""
    await _prime(fake_storage_provider)

    class _Down(_Ws):
        async def file_info(self, path):
            raise WorkspaceUnreachableError("the runtime did not answer")

    ws = _Down()
    with pytest.raises(WorkspaceUnreachableError):
        await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws))
    assert ws.writes == []


async def test_a_failing_check_of_the_second_name_writes_nothing(fake_storage_provider):
    """``agents.md`` is absent but ``AGENTS.md`` cannot be checked: it may be there, so nothing is written."""
    await _prime(fake_storage_provider)

    class _HalfDown(_Ws):
        async def file_info(self, path):
            if path == "AGENTS.md":
                raise WorkspaceUnreachableError("the runtime did not answer")
            return await super().file_info(path)

    ws = _HalfDown()
    with pytest.raises(WorkspaceUnreachableError):
        await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws))
    assert ws.writes == []


def _one_warning(caplog) -> logging.LogRecord:
    """The one record the seed logged: a WARNING, without a traceback."""
    records = [r for r in caplog.records if r.name == prime_module.logger.name and r.levelno >= logging.WARNING]
    assert len(records) == 1, [r.getMessage() for r in records]
    assert records[0].levelno == logging.WARNING and records[0].exc_info is None
    return records[0]


async def test_a_check_that_hangs_writes_nothing_and_warns_once(fake_storage_provider, monkeypatch, caplog):
    """A stuck backend must not hold the boot: reaching the workspace and the check share one bound; past it nothing is written, the
    step answers False with one WARNING, and the next boot tries again."""
    await _prime(fake_storage_provider)
    monkeypatch.setattr(prime_module, "PRIME_AGENTS_MD_CHECK_TIMEOUT_S", 0.05)

    class _Stuck(_Ws):
        async def file_info(self, path):
            await asyncio.Event().wait()

    ws = _Stuck()
    async with asyncio.timeout(5):
        assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws)) is False
    assert ws.writes == []
    assert "next boot" in _one_warning(caplog).getMessage()


async def test_a_reattach_that_hangs_is_under_the_same_bound(fake_storage_provider, monkeypatch, caplog):
    """``get_workspace`` re-attaches to the workspace's backend (on Kubernetes it can wait for a Pending pod): it is inside the bound."""
    await _prime(fake_storage_provider)
    monkeypatch.setattr(prime_module, "PRIME_AGENTS_MD_CHECK_TIMEOUT_S", 0.05)

    class _HungReattach:
        async def get_workspace(self, workspace_id):
            await asyncio.Event().wait()

    started = time.monotonic()
    async with asyncio.timeout(5):
        assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_HungReattach()) is False
    assert time.monotonic() - started < 2
    _one_warning(caplog)


async def test_a_write_that_hangs_is_bounded_on_its_own(fake_storage_provider, monkeypatch, caplog):
    """The write has its own bound of the same length. A write given up on may still land later: it writes the same shipped bytes."""
    await _prime(fake_storage_provider)
    monkeypatch.setattr(prime_module, "PRIME_AGENTS_MD_CHECK_TIMEOUT_S", 0.05)

    class _HungWrite(_Ws):
        async def write_file(self, path, content):
            self.writes.append(path)
            await asyncio.Event().wait()

    ws = _HungWrite()
    async with asyncio.timeout(5):
        assert await ensure_prime_agents_md(fake_storage_provider, workspace_registry=_Registry(ws)) is False
    assert ws.writes == ["agents.md"]
    assert "may still" in _one_warning(caplog).getMessage()


async def test_a_hung_reattach_does_not_hold_the_ensure_pass(fake_storage_provider, monkeypatch, caplog):
    """The boot awaits the whole pass before it serves: with prime's backend stuck, the pass still completes within the bound."""
    await _prime(fake_storage_provider)
    monkeypatch.setattr(prime_module, "PRIME_AGENTS_MD_CHECK_TIMEOUT_S", 0.05)

    class _HungReattach:
        async def get_workspace(self, workspace_id):
            await asyncio.Event().wait()

    async with asyncio.timeout(5):
        result = await run_ensure_pass(fake_storage_provider, workspace_registry=_HungReattach())
    assert result.errors == []
    _one_warning(caplog)


# ---- the ensure pass --------------------------------------------------------------------------------------------------------------


async def test_the_ensure_pass_seeds_it_and_a_second_pass_keeps_an_edit(fake_storage_provider):
    await _prime(fake_storage_provider)
    ws = _Ws()
    first = await run_ensure_pass(fake_storage_provider, workspace_registry=_Registry(ws))
    assert first.errors == []
    assert ws.files["agents.md"] == shipped_prime_agents_md()

    ws.files["agents.md"] = b"edited by a person"
    second = await run_ensure_pass(fake_storage_provider, workspace_registry=_Registry(ws))
    assert second.errors == []
    assert ws.files["agents.md"] == b"edited by a person" and ws.writes == ["agents.md"]


async def test_a_failing_seed_is_recorded_under_its_step_and_the_others_run(fake_storage_provider):
    await _prime(fake_storage_provider)

    class _Down(_Ws):
        async def file_info(self, path):
            raise WorkspaceUnreachableError("the runtime did not answer")

    result = await run_ensure_pass(fake_storage_provider, workspace_registry=_Registry(_Down()))
    assert [step for step, _ in result.errors] == ["prime_agents_md"]
    # The step after it still ran: the system collection exists.
    from primer.model.collection import Collection

    assert await fake_storage_provider.get_storage(Collection).get("system") is not None


# ---- the production shape: a real local workspace -----------------------------------------------------------------------------------


@pytest.fixture
async def local_prime(fake_storage_provider, tmp_path):
    """The default workspace on a real local provider, created by the ensure step, behind the real ``WorkspaceRegistry``."""
    from primer.api.registries import WorkspaceRegistry
    from primer.bootstrap.defaults import (
        RESERVED_LOCAL_WORKSPACE_PROVIDER,
        RESERVED_LOCAL_WORKSPACE_TEMPLATE,
        RESERVED_WORKSPACE_TEMPLATES,
    )
    from primer.bootstrap.seed import ensure_default_workspace
    from primer.model.workspace import (
        LocalWorkspaceConfig,
        WorkspaceProvider,
        WorkspaceProviderType,
        WorkspaceTemplate,
    )

    sp = fake_storage_provider
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(**RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE])
    )
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(
            id=RESERVED_LOCAL_WORKSPACE_PROVIDER,
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path=str(tmp_path / "ws")),
        )
    )
    registry = WorkspaceRegistry(sp)
    try:
        created = await ensure_default_workspace(sp, workspace_registry=registry)
        assert created == "prime"
        yield sp, registry
    finally:
        await registry.aclose()


async def test_on_a_real_workspace_the_boot_writes_the_file_the_loader_reads(local_prime):
    sp, registry = local_prime
    assert await ensure_prime_agents_md(sp, workspace_registry=registry) is True
    ws = await registry.get_workspace("prime")
    assert await ws.read_file("agents.md") == shipped_prime_agents_md()
    assert await load_workspace_instructions(ws) == shipped_prime_agents_md().decode("utf-8")
    # A second boot finds it and writes nothing.
    await ws.write_file("agents.md", b"edited")
    assert await ensure_prime_agents_md(sp, workspace_registry=registry) is False
    assert await ws.read_file("agents.md") == b"edited"


async def test_on_a_real_workspace_an_AGENTS_md_is_not_shadowed(local_prime):
    sp, registry = local_prime
    ws = await registry.get_workspace("prime")
    await ws.write_file("AGENTS.md", b"the team's own instructions")
    assert await ensure_prime_agents_md(sp, workspace_registry=registry) is False
    with pytest.raises(NotFoundError):
        await ws.file_info("agents.md")
    assert await load_workspace_instructions(ws) == "the team's own instructions"


# ---- symlinks: written where they point (documented; an lstat-based refusal is a separate ticket) -------------------------------------


def _root(ws) -> str:
    return str(ws._root.resolve())  # noqa: SLF001 - the test plants a link the platform has no API for


async def test_a_dangling_agents_md_link_is_absent_and_the_seed_writes_where_it_points(local_prime):
    """``file_info`` follows the link, so a dangling one reads as absent, and the write lands on its target."""
    sp, registry = local_prime
    ws = await registry.get_workspace("prime")
    os.symlink("missing.md", f"{_root(ws)}/agents.md")
    assert await ensure_prime_agents_md(sp, workspace_registry=registry) is True
    assert os.path.islink(f"{_root(ws)}/agents.md")
    assert await ws.read_file("missing.md") == shipped_prime_agents_md()


async def test_the_reset_writes_through_an_agents_md_link_to_AGENTS_md(local_prime):
    """The common "both names" link: ``agents.md -> AGENTS.md``. The reset writes where it points, so AGENTS.md is rewritten."""
    sp, registry = local_prime
    ws = await registry.get_workspace("prime")
    await ws.write_file("AGENTS.md", b"the team's own instructions")
    os.symlink("AGENTS.md", f"{_root(ws)}/agents.md")
    await reset_prime_agents_md(sp, workspace_registry=registry)
    assert os.path.islink(f"{_root(ws)}/agents.md")
    assert await ws.read_file("AGENTS.md") == shipped_prime_agents_md()
