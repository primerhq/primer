"""End-to-end integration test for the local workspace backend.

Walks the "End-to-end user flow" described in
``docs/superpowers/specs/2026-05-02-workspace-design.md`` step-by-step:
materialise a workspace, drive multiple sessions through their full
lifecycle, exercise the seven workspace tools, append instructions
mid-run, traverse all four SessionStatus states, end the session, and
start a fresh session on the same workspace.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from matrix.model.workspace_session import (
    AgentBinding,
    SessionStatus,
    _ToolApprovalWaiting,  # type: ignore[attr-defined]
    _UserInputWaiting,  # type: ignore[attr-defined]
)
from matrix.model.workspace import (
    FileMount,
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from matrix.workspace import (
    AgentSession,
    LocalWorkspace,
    LocalWorkspaceBackend,
    ToolCallContext,
    WorkspaceBackendFactory,
)
from matrix.workspace.local.tools import EditArgs, ExecArgs, GrepArgs, ReadArgs


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


# ===========================================================================
# Helpers
# ===========================================================================


def _build_template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="research",
        description="Research workstation",
        provider_id="local-1",
        files=[
            FileMount(
                path="src/main.py",
                source={
                    "kind": "inline",
                    "content": "def main():\n    return 'TODO: implement'\n",
                },
            ),
            FileMount(
                path="README.md",
                source={
                    "kind": "inline",
                    "content": "# Research repo\n\nslow_test in tests/test_x.py\n",
                },
            ),
        ],
        init_commands=[
            f'"{sys.executable}" -c '
            f'"import os; os.makedirs(\'tests\', exist_ok=True); '
            f'open(\'tests/test_x.py\',\'w\').write(\'def slow_test(): pass\\n\')"'
        ],
    )


def _ctx_for(session: AgentSession, *, call_id: str = "c-1") -> ToolCallContext:
    return ToolCallContext(
        workspace_id=session.workspace_id,
        session_id=session.session_id,
        agent_id=session.agent_id,
        call_id=call_id,
        abort=asyncio.Event(),
        session=session,
    )


def _tool(session: AgentSession, tool_id: str):
    """Find a workspace tool by id on a session."""
    for t in session.workspace_tools:
        if t.id == tool_id:
            return t
    raise AssertionError(f"workspace tool {tool_id!r} not in session")


# ===========================================================================
# The end-to-end test
# ===========================================================================


async def test_end_to_end_user_flow(tmp_path: Path) -> None:
    """Walk the full flow described in the spec."""

    # 1-2. User defines an agent and picks a workspace template.
    # Wire the local backend the way the runtime would: a WorkspaceProvider
    # config entry routed through WorkspaceBackendFactory.
    backend_config = WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(path=str(tmp_path / "provider_root")),
    )
    provider = WorkspaceBackendFactory.create(backend_config)
    assert isinstance(provider, LocalWorkspaceBackend)
    await provider.initialize()
    template = _build_template()
    assert template.provider_id == backend_config.id

    # 3. User materialises a workspace.
    overrides = WorkspaceTemplateOverrides(
        env={"EXTRA_VAR": "from-overrides"},
    )
    workspace = await provider.create(template, overrides=overrides)
    assert isinstance(workspace, LocalWorkspace)
    assert (workspace.root / "src" / "main.py").exists()
    assert (workspace.root / "tests" / "test_x.py").exists()

    # 4. User starts a session (the runtime would build the binding from
    # the agent definition; here we construct it directly).
    binding = AgentBinding(
        agent_id="researcher",
        agent_name="Research Agent",
        registered_tool_ids=["find_tool", "call_tool"],
    )
    session = await workspace.start_session(
        binding,
        instructions="Find the slowest test and propose a fix.",
    )
    assert await session.status() == SessionStatus.RUNNING
    pending = await session.take_pending_messages()
    assert len(pending) == 1
    assert pending[0].role == "user"

    # 5. Runtime drives the session: dispatch a few workspace tools.
    ctx = _ctx_for(session, call_id="c-grep-1")
    grep = _tool(session, "grep")
    grep_result = await grep.execute(
        GrepArgs(pattern="slow_test", path=".", output_mode="content"),
        ctx,
    )
    assert "test_x.py" in grep_result.output

    # The runtime would commit_state after the turn; mimic with a
    # dummy assistant message so take_pending_messages's cursor advances.
    msgs_path = (
        workspace.root / template.state_path / "sessions" / session.session_id
        / "messages.jsonl"
    )
    existing = msgs_path.read_text(encoding="utf-8")
    new_jsonl = existing + (
        '{"role":"assistant","parts":[{"type":"text","text":"Looking at test_x.py..."}]}\n'
    )
    await session.commit_state(
        summary=f"{session.session_id}: assistant turn 1",
        op="message",
        files={"messages.jsonl": new_jsonl},
    )
    assert await session.take_pending_messages() == []

    # Read the file (marks it as read so a later write doesn't trip the
    # read-before-write rule), then edit it via the workspace tool.
    read_ctx = _ctx_for(session, call_id="c-read-1")
    await _tool(session, "read").execute(
        ReadArgs(path="src/main.py"), read_ctx
    )
    edit_ctx = _ctx_for(session, call_id="c-edit-1")
    edit_result = await _tool(session, "edit").execute(
        EditArgs(
            path="src/main.py",
            old_string="TODO: implement",
            new_string="42  # answered",
        ),
        edit_ctx,
    )
    assert "+    return '42  # answered'" in edit_result.output

    # Run a quick command via exec; per-session env was injected.
    exec_ctx = _ctx_for(session, call_id="c-exec-1")
    exec_result = await _tool(session, "exec").execute(
        ExecArgs(
            command=(
                f'"{sys.executable}" -c '
                '"import os; print(os.environ.get(\'EXTRA_VAR\', \'?\'))"'
            ),
            description="check env propagation",
        ),
        exec_ctx,
    )
    assert "from-overrides" in exec_result.output

    # 5a. Verify per-session .tmp scoping: cache_output writes under
    # .tmp/<session_id>/, not the workspace root.
    cached_path = await session.cache_output("a large blob from a tool")
    cached = Path(cached_path)
    assert cached.parent == workspace.root / template.tmp_path / session.session_id

    # 6. User appends an instruction mid-run.
    instr = await session.append_instruction(
        "Also check pytest plugins for slow imports."
    )
    assert instr.session_id == session.session_id
    pending = await session.take_pending_messages()
    assert any(
        any(getattr(p, "text", "") == instr.content for p in m.parts)
        for m in pending
    )

    # 7. User pauses temporarily.
    await session.request_pause(reason="Want to think about scope.")
    assert session.pause_requested is True
    # Runtime would observe the flag and call set_status; we mimic.
    await session.set_status(SessionStatus.PAUSED)
    assert await session.status() == SessionStatus.PAUSED

    # 8. User resumes.
    await session.request_resume()
    assert session.pause_requested is False
    await session.set_status(SessionStatus.RUNNING)
    assert await session.status() == SessionStatus.RUNNING

    # 10. Runtime transitions to WAITING (both kinds, separately).
    await session.set_status(
        SessionStatus.WAITING,
        waiting_state=_UserInputWaiting(
            prompt="Which scope: full repo or src/ only?",
            queued_at=datetime.now(timezone.utc),
        ),
    )
    waiting = await session.waiting_state()
    assert waiting is not None and waiting.kind == "user_input"

    # User responds via append_instruction; runtime clears the wait.
    await session.append_instruction("src/ only please")
    await session.set_status(SessionStatus.RUNNING)
    waiting_path = (
        workspace.root / template.state_path / "sessions" / session.session_id
        / "waiting.json"
    )
    assert not waiting_path.exists()

    # Re-enter waiting with a tool_approval kind to exercise that branch.
    await session.set_status(
        SessionStatus.WAITING,
        waiting_state=_ToolApprovalWaiting(
            tool_id="exec",
            arguments={"command": "rm -rf /tmp/scratch"},
            rationale="Cleanup before fresh build.",
            queued_at=datetime.now(timezone.utc),
        ),
    )
    waiting = await session.waiting_state()
    assert waiting is not None and waiting.kind == "tool_approval"

    # 9. Browse workspace files.
    entries = await workspace.list_files(".", recursive=True)
    rels = {e.path for e in entries}
    assert "src/main.py" in rels
    assert "README.md" in rels

    contents = await workspace.read_file("src/main.py")
    assert b"42  # answered" in contents

    chunks = bytearray()
    async for chunk in workspace.download_archive(paths=["src", "README.md"]):
        chunks.extend(chunk)
    with tarfile.open(fileobj=io.BytesIO(bytes(chunks)), mode="r") as tf:
        names = tf.getnames()
    assert "README.md" in names
    assert any(n.startswith("src/") for n in names)

    # 12. End the session (transitions WAITING -> ENDED in one call).
    sess_tmp = workspace.root / template.tmp_path / session.session_id
    assert sess_tmp.is_dir()
    await session.aclose()
    assert await session.status() == SessionStatus.ENDED
    assert (await session.info()).ended_reason == "completed"
    assert not sess_tmp.exists()  # per-session tmp reaped

    # 13. Start a second session of the same agent on the same workspace.
    second = await workspace.start_session(binding)
    assert second.session_id != session.session_id
    assert await second.status() == SessionStatus.RUNNING

    sessions = await workspace.list_sessions()
    assert {s.session_id for s in sessions} == {
        session.session_id,
        second.session_id,
    }

    # Tear everything down via the provider.
    await second.aclose()
    await provider.destroy(workspace.id)
    assert workspace.id not in await provider.list()
    await provider.aclose()
