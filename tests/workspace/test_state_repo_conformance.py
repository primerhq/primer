"""StateRepo conformance suite.

Parametrized over StateRepo implementations.  The "local" parameter
exercises :class:`LocalStateRepo`; the "sandbox" parameter exercises
:class:`SandboxStateRepo` over a real container runtime (Docker) and is
skipped when the ``workspace:container`` capability is absent.

Each test asserts a behavioural invariant that every conforming StateRepo
implementation must satisfy.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.model.workspace import CommitInfo
from primer.model.workspace_session import (
    AgentBinding,
    SessionInfo,
    SessionStatus,
)
from tests._support.testconfig import caps


# ===========================================================================
# Skip guard
# ===========================================================================


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _git_available(),
    reason="git CLI not available on PATH",
)


# ===========================================================================
# Builders
# ===========================================================================


def _make_session_info(
    *,
    session_id: str = "sess-conform-1",
    agent_id: str = "agent-conform",
    workspace_id: str = "ws-conform",
    status: SessionStatus = SessionStatus.RUNNING,
) -> SessionInfo:
    now = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
    return SessionInfo(
        session_id=session_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        status=status,
        started_at=now,
        last_activity_at=now,
    )


def _make_binding(
    *,
    agent_id: str = "agent-conform",
    agent_name: str = "Conform Agent",
) -> AgentBinding:
    return AgentBinding(agent_id=agent_id, agent_name=agent_name)


# ===========================================================================
# Parametrized fixture
# ===========================================================================


@pytest.fixture(params=["local", "sandbox"])
async def state_repo(request, tmp_path: Path):
    """Yield an initialized StateRepo for the implementation under test.

    * ``local``   -- :class:`LocalStateRepo` over a tmp_path git repo.
    * ``sandbox`` -- :class:`SandboxStateRepo` over a real Docker container
                     running ``primer/workspace-runtime:1.1``.  Skipped when
                     the ``workspace:container`` capability is absent.
    """
    impl = request.param

    if impl == "local":
        from primer.workspace.local.state import LocalStateRepo

        repo = LocalStateRepo(tmp_path / ".state", workspace_id="ws-conform")
        await repo.initialize()
        yield repo

    elif impl == "sandbox":
        # Gate: skip when Docker / container backend is not available.
        if not caps().has("workspace:container"):
            pytest.skip("workspace:container capability not available")

        import secrets
        from primer.model.workspace import (
            ContainerConnectionSocket,
            ContainerReachabilityHostPort,
            ContainerWorkspaceConfig,
        )
        from primer.workspace.runtime.docker import DockerRuntimeAdapter
        from primer.workspace.sandbox.state import SandboxStateRepo

        _RUNTIME_IMAGE = "primer/workspace-runtime:1.1"
        # Use a unique id per fixture instance so parallel/sequential test
        # runs do not collide on container or volume names.
        _UNIQ = secrets.token_hex(8)
        _WORKSPACE_ID = f"ws-conform-{_UNIQ}"
        _NAME = f"workspace-{_WORKSPACE_ID}"
        _VOLUME = f"{_NAME}-data"

        cfg = ContainerWorkspaceConfig(
            kind="container",
            runtime="docker",
            connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
            reachability=ContainerReachabilityHostPort(bind_host="127.0.0.1"),
        )
        adapter = DockerRuntimeAdapter(cfg)
        await adapter.initialize()

        from primer.model.workspace import ResourceLimits

        token = secrets.token_urlsafe(32)
        try:
            sandbox = await adapter.create_sandbox(
                name=_NAME,
                image=_RUNTIME_IMAGE,
                command=["python", "-m", "primer_runtime.server"],
                env={},
                workdir="/workspace",
                volume_name=_VOLUME,
                volume_target="/workspace",
                extra_mounts=[],
                user=None,
                resources=ResourceLimits(),
                network="full",
                pull_policy="if_missing",
                reachability=ContainerReachabilityHostPort(bind_host="127.0.0.1"),
                token=token,
            )
        except Exception:
            await adapter.aclose()
            raise

        repo = SandboxStateRepo(
            sandbox,
            state_path="/workspace/.state",
            workspace_id=_WORKSPACE_ID,
        )
        await repo.initialize()

        try:
            yield repo
        finally:
            # Teardown: stop + remove the container and its volume.
            try:
                await sandbox.stop()
            except Exception:
                pass
            try:
                await sandbox.remove()
            except Exception:
                pass
            try:
                await adapter.remove_volume(_VOLUME)
            except Exception:
                pass
            await adapter.aclose()

    else:
        pytest.skip(f"unknown impl param: {impl!r}")


# ===========================================================================
# Conformance tests
# ===========================================================================


async def test_create_session_then_load_round_trips(state_repo) -> None:
    """create_session writes session.json + agent.json; load_session_info and
    load_agent_binding round-trip the load-bearing fields."""
    info = _make_session_info()
    binding = _make_binding()

    sha = await state_repo.create_session(info, binding)
    assert isinstance(sha, str) and len(sha) == 40, "SHA must be a 40-char hex string"

    loaded_info = await state_repo.load_session_info(info.session_id)
    assert loaded_info is not None, "load_session_info returned None after create_session"
    assert loaded_info.session_id == info.session_id
    assert loaded_info.agent_id == info.agent_id
    assert loaded_info.workspace_id == info.workspace_id
    assert loaded_info.status == info.status

    loaded_binding = await state_repo.load_agent_binding(info.session_id)
    assert loaded_binding is not None, "load_agent_binding returned None after create_session"
    assert loaded_binding.agent_id == binding.agent_id


async def test_commit_then_history(state_repo) -> None:
    """commit() records a CommitInfo visible in history(); subject and op trailer
    are preserved."""
    info = _make_session_info()
    binding = _make_binding()
    await state_repo.create_session(info, binding)

    sha = await state_repo.commit(
        info.session_id,
        summary="conform: message turn",
        op="message",
        files={"messages.jsonl": '{"role":"assistant","content":"hello"}\n'},
    )
    assert isinstance(sha, str) and len(sha) == 40

    records = await state_repo.history(session_id=info.session_id, limit=10)
    assert records, "history() returned empty after a commit"

    subjects = [c.subject for c in records]
    assert "conform: message turn" in subjects, (
        f"expected commit subject not found in history; got: {subjects!r}"
    )

    # Find the specific commit record we just made.
    target = next((c for c in records if c.subject == "conform: message turn"), None)
    assert target is not None
    assert isinstance(target, CommitInfo)
    assert target.op == "message"
    assert target.session_id == info.session_id
    assert target.agent_id == binding.agent_id


async def test_commit_arbitrary_then_read_state_file(state_repo) -> None:
    """commit_arbitrary() writes files at repo-root-relative paths;
    read_state_file() returns the same bytes."""
    payload = b'{"graph_id": "g-1", "step": 0}'
    path = "graphs/g-1/state.json"

    sha = await state_repo.commit_arbitrary(
        summary="graph state init",
        files={path: payload},
    )
    assert isinstance(sha, str) and len(sha) == 40

    result = await state_repo.read_state_file(path)
    assert result is not None, "read_state_file returned None after commit_arbitrary"
    assert result == payload, (
        f"read_state_file returned {result!r}, expected {payload!r}"
    )


async def test_load_waiting_state_round_trips(state_repo) -> None:
    """Writing waiting.json via commit() makes load_waiting_state() return
    the parsed WaitingState; before that commit it returns None.

    Note: this drives waiting state directly via commit() with a
    waiting.json file, which is the same mechanism AgentSession uses.
    The file must carry a valid WaitingState JSON payload.
    """
    info = _make_session_info()
    binding = _make_binding()
    await state_repo.create_session(info, binding)

    # Before any waiting.json exists, must return None.
    ws_before = await state_repo.load_waiting_state(info.session_id)
    assert ws_before is None, (
        f"expected None for non-waiting session, got {ws_before!r}"
    )

    # Write a valid user_input waiting state.
    waiting_payload = json.dumps({
        "kind": "user_input",
        "prompt": "What is the answer?",
        "queued_at": "2026-05-02T10:00:00+00:00",
    })
    await state_repo.commit(
        info.session_id,
        summary="enter waiting",
        op="status_change",
        files={"waiting.json": waiting_payload},
    )

    ws = await state_repo.load_waiting_state(info.session_id)
    assert ws is not None, "load_waiting_state returned None after writing waiting.json"
    assert ws.kind == "user_input"
    assert ws.prompt == "What is the answer?"
