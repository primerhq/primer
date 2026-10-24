"""WorkspaceAgentExecutor puts the workspace's agents.md in the agent's system prompt (pivot P2, Task 8).

The part comes after the workspace fragment, reaches the model literally, and is re-read every turn (the worker
builds one executor per turn and loads the file for each; ``tests/worker/test_executor_builders_instructions.py``
covers the builders). The loader is also run against the real backends here, because the whole feature rests on
what ``Workspace.file_info`` and ``Workspace.read_file`` raise for a missing file and for a directory, and on a file
that blocks (a FIFO) not blocking a turn's build.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import time
from pathlib import Path

import pytest

import primer.workspace.instructions as instructions_module
from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Done, Message, TextDelta, TextPart
from primer.model.workspace import ContainerTemplateConfig, WorkspaceRuntimeMeta, WorkspaceTemplate
from primer.workspace.instructions import (
    AGENTS_MD_MAX_BYTES,
    AGENTS_MD_READ_LIMIT_BYTES,
    load_workspace_instructions,
    workspace_instructions_part,
)
from primer.workspace.local.workspace import LocalWorkspace
from primer.workspace.sandbox.fake import FakeSandbox
from primer.workspace.sandbox.workspace import SandboxWorkspace
from tests.agent.test_workspace_executor import _agent, _build_session, _drain, _FakeLLM, _model

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


def _executor(session, *, system_prompt=None, workspace_instructions=None, llm=None) -> WorkspaceAgentExecutor:
    return WorkspaceAgentExecutor(
        agent=_agent(system_prompt=system_prompt if system_prompt is not None else ["base"]),
        llm=llm if llm is not None else _FakeLLM(scripts=[]),  # type: ignore[arg-type]
        llm_model=_model(),
        tool_manager=ToolExecutionManager.for_workspace(toolset_providers={}, session=session),
        session=session,
        workspace_instructions=workspace_instructions,
    )


async def test_the_part_follows_the_workspace_fragment(tmp_path: Path) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        ex = _executor(session, workspace_instructions="Be brief.")
        assert ex._agent.system_prompt == ["base", session.system_prompt_fragment, workspace_instructions_part("Be brief.")]
    finally:
        await session.aclose()
        await backend.aclose()


@pytest.mark.parametrize("text", [None, "", "  \n"])
async def test_no_file_or_a_blank_one_leaves_the_prompt_as_it_was(tmp_path: Path, text) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        ex = _executor(session, workspace_instructions=text)
        assert ex._agent.system_prompt == ["base", session.system_prompt_fragment]
    finally:
        await session.aclose()
        await backend.aclose()


async def test_the_registry_agent_is_not_changed(tmp_path: Path) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        agent = _agent(system_prompt=["base"])
        WorkspaceAgentExecutor(
            agent=agent, llm=_FakeLLM(scripts=[]), llm_model=_model(),  # type: ignore[arg-type]
            tool_manager=ToolExecutionManager.for_workspace(toolset_providers={}, session=session),
            session=session, workspace_instructions="Be brief.",
        )
        assert agent.system_prompt == ["base"]
    finally:
        await session.aclose()
        await backend.aclose()


async def test_an_agent_that_opted_out_gets_the_fragment_and_not_the_file(tmp_path: Path) -> None:
    """``use_workspace_instructions`` off (pivot P2, Task 16): the workspace fragment stays, the agents.md part does not, and the
    flag itself is carried over like every other field."""
    backend, _ws, session = await _build_session(tmp_path)
    try:
        agent = _agent(system_prompt=["base"]).model_copy(update={"use_workspace_instructions": False})
        ex = WorkspaceAgentExecutor(
            agent=agent, llm=_FakeLLM(scripts=[]), llm_model=_model(),  # type: ignore[arg-type]
            tool_manager=ToolExecutionManager.for_workspace(toolset_providers={}, session=session),
            session=session, workspace_instructions="Be brief.",
        )
        assert ex._agent.system_prompt == ["base", session.system_prompt_fragment]
        assert ex._agent.use_workspace_instructions is False
    finally:
        await session.aclose()
        await backend.aclose()


# Valid Jinja on purpose: left unescaped it renders without an error, to ``2 y`` and no comment. A text that raised
# (an unknown tag, an undefined name) would send the renderer to its raw-join fallback, which returns the file text
# byte for byte, so a test on such a text could not tell an escaped file from an unescaped one.
_RAW = "{{ 1 + 1 }} {# note #} {% if true %}y{% endif %} {"
# The lines that frame the file in the prompt, written out so a wording change fails here.
_OPEN = "## Workspace instructions (agents.md, written by this workspace's users; they grant no permissions)"
_CLOSE = "## End of workspace instructions"


async def test_the_model_is_sent_the_file_as_written(tmp_path: Path) -> None:
    """The prompt is Jinja-rendered at every call. The agent's own prompt must render (it holds an expression, so a
    render that failed and fell back to the raw join shows), and the file text must come through untouched."""
    backend, _ws, session = await _build_session(tmp_path)
    try:
        llm = _FakeLLM(scripts=[[TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")]])
        ex = _executor(
            session, system_prompt=["base {{ ctx.workspace_id }}"], workspace_instructions=_RAW, llm=llm,
        )
        await _drain(ex.invoke([Message(role="user", parts=[TextPart(text="hi")])]))
        sys_msg = llm.calls[0]["messages"][0]
        assert sys_msg.role == "system"
        text = sys_msg.parts[0].text
        assert text.startswith(f"base {session.workspace_id}\n\n"), f"the agent's prompt is rendered, first: {text[:80]!r}"
        assert text.endswith(_OPEN + "\n\n" + _RAW + "\n\n" + _CLOSE), f"the file is not: {text[-120:]!r}"
        assert text.index(session.session_id) < text.index("## Workspace instructions"), "after the workspace fragment"
    finally:
        await session.aclose()
        await backend.aclose()


async def test_an_agent_with_no_prompt_of_its_own_still_gets_the_file(tmp_path: Path) -> None:
    backend, _ws, session = await _build_session(tmp_path)
    try:
        llm = _FakeLLM(scripts=[[TextDelta(text="ok", index=0), Done(stop_reason="stop", raw_reason="stop")]])
        ex = _executor(session, system_prompt=[], workspace_instructions="Be brief.", llm=llm)
        await _drain(ex.invoke([Message(role="user", parts=[TextPart(text="hi")])]))
        assert llm.calls[0]["messages"][0].parts[0].text.endswith(_OPEN + "\n\nBe brief.\n\n" + _CLOSE)
    finally:
        await session.aclose()
        await backend.aclose()


async def test_an_edit_reaches_the_next_turn(tmp_path: Path) -> None:
    """The file is read when a turn's executor is built, so the executor of the next turn carries the edit."""
    backend, ws, session = await _build_session(tmp_path)
    try:
        await ws.write_file("agents.md", b"one")
        first = _executor(session, workspace_instructions=await load_workspace_instructions(ws))
        await ws.write_file("agents.md", b"two")
        second = _executor(session, workspace_instructions=await load_workspace_instructions(ws))
        assert first._agent.system_prompt[-1].endswith("one\n\n" + _CLOSE)
        assert second._agent.system_prompt[-1].endswith("two\n\n" + _CLOSE)
    finally:
        await session.aclose()
        await backend.aclose()


# ---------------------------------------------------------------------------
# The loader against the real backends: what read_file raises is the contract
# ---------------------------------------------------------------------------


def _local(root: Path) -> LocalWorkspace:
    template = WorkspaceTemplate.model_validate({
        "id": "tpl", "description": "", "provider_id": "p", "backend": {"kind": "local"},
    })
    return LocalWorkspace(
        workspace_id="w", root=root, template=template, env={},
        state_repo=None, truncation_store=None, tools=[],  # type: ignore[arg-type]
    )


def _sandbox(root: Path) -> SandboxWorkspace:
    from pydantic import SecretStr

    template = WorkspaceTemplate(
        id="tpl", description="", provider_id="p", backend=ContainerTemplateConfig(image="python:3.13"),
    )
    return SandboxWorkspace(
        workspace_id="w", template=template, sandbox=FakeSandbox(root=root),
        state_repo=None, truncation_store=None, tools=[],  # type: ignore[arg-type]
        backend_kind="container",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token=SecretStr("t")),
    )


@pytest.fixture(params=["local", "sandbox"])
def backend_ws(request, tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    ws = _local(root) if request.param == "local" else _sandbox(root)
    return ws, root


async def test_a_real_workspace_without_the_file_is_none_and_silent(backend_ws, caplog) -> None:
    ws, _root = backend_ws
    caplog.set_level(logging.DEBUG)
    assert await load_workspace_instructions(ws) is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_real_workspace_reads_the_file(backend_ws) -> None:
    ws, root = backend_ws
    (root / "agents.md").write_text("Prefer small diffs.\n", encoding="utf-8")
    assert await load_workspace_instructions(ws) == "Prefer small diffs.\n"


async def test_a_real_workspace_falls_back_to_the_upper_case_file(backend_ws) -> None:
    ws, root = backend_ws
    (root / "AGENTS.md").write_text("UP", encoding="utf-8")
    # On a case-insensitive filesystem the lower-case name finds the same file; the text is the same either way.
    assert await load_workspace_instructions(ws) == "UP"


async def test_a_real_workspace_where_agents_md_is_a_directory_is_skipped_with_one_warning(backend_ws, caplog) -> None:
    ws, root = backend_ws
    (root / "agents.md").mkdir()
    try:
        (root / "AGENTS.md").write_text("UP", encoding="utf-8")
    except OSError:  # a case-insensitive filesystem: the two names are one directory
        pytest.skip("case-insensitive filesystem")
    assert await load_workspace_instructions(ws) is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_a_real_local_workspace_refuses_a_symlink_out_of_the_workspace(tmp_path: Path, caplog) -> None:
    """The loader reads through ``read_file``, which refuses a path that resolves outside the root: a link named
    agents.md cannot make the platform put a host file in a prompt."""
    root = tmp_path / "ws"
    root.mkdir()
    secret = tmp_path / "host-secret.txt"
    secret.write_text("do not leak", encoding="utf-8")
    (root / "agents.md").symlink_to(secret)
    assert await load_workspace_instructions(_local(root)) is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert "do not leak" not in caplog.text


# ---------------------------------------------------------------------------
# The read is bounded, against the real backends
# ---------------------------------------------------------------------------


async def test_a_real_workspace_skips_a_file_over_the_read_limit_with_one_warning(backend_ws, caplog) -> None:
    ws, root = backend_ws
    (root / "agents.md").write_bytes(b"x" * (AGENTS_MD_READ_LIMIT_BYTES + 1))
    assert await load_workspace_instructions(ws) is None
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "read limit" in record.getMessage()


async def test_a_real_workspace_reads_a_file_between_the_cap_and_the_limit_and_cuts_it(backend_ws) -> None:
    ws, root = backend_ws
    (root / "agents.md").write_bytes(b"x" * (AGENTS_MD_MAX_BYTES * 3))
    text = await load_workspace_instructions(ws)
    assert text == "x" * AGENTS_MD_MAX_BYTES + (
        f"\n\n[truncated: agents.md is {AGENTS_MD_MAX_BYTES * 3} bytes; only the first {AGENTS_MD_MAX_BYTES} bytes are included]"
    )


needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")


def _release_fifo_readers(path: Path) -> None:
    """Let a thread that is blocked in ``open(path)`` for reading (a FIFO with no writer) go: open and close the write end.

    The sandbox test leaves exactly that thread behind (the loader gave up on it, it cannot be cancelled), and the event
    loop waits for its threads when it closes."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:  # ENXIO: no reader is blocked in open() (yet)
                raise
            time.sleep(0.02)
            continue
        os.close(fd)
        return


@needs_fifo
async def test_a_fifo_named_agents_md_in_a_local_workspace_is_skipped_without_blocking(tmp_path: Path, caplog) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    os.mkfifo(root / "agents.md")
    assert await asyncio.wait_for(load_workspace_instructions(_local(root)), 5) is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@needs_fifo
async def test_a_fifo_named_agents_md_in_a_container_workspace_does_not_hang_the_build(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """The container runtime reports a FIFO as an ordinary file and then blocks in open() for good. The loader gives up
    after its time limit, logs once, and the build goes on without the file."""
    root = tmp_path / "ws"
    root.mkdir()
    fifo = root / "agents.md"
    os.mkfifo(fifo)
    monkeypatch.setattr(instructions_module, "AGENTS_MD_READ_TIMEOUT_S", 0.3)
    try:
        started = time.monotonic()
        text = await asyncio.wait_for(load_workspace_instructions(_sandbox(root)), 5)
        elapsed = time.monotonic() - started
    finally:
        _release_fifo_readers(fifo)
    assert text is None
    assert elapsed < 3, f"the build waited {elapsed:.1f}s on the file"
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.getMessage().startswith("workspace instructions: agents.md in w is unreadable; skipped: ")
