"""A run of a session is ranked by its creator's CURRENT role, capped by the recorded one (lead ruling 2 of the G7 fixes, audit G7-06 and G6-6).

Every turn and every resume used to rank by ``WorkspaceSession.initiated_by`` as it was recorded when the session was created. Nothing refreshed it, so a
demoted admin's session kept running admin tools, an admin-tool call parked on an approval re-ran as admin when it was approved after the demotion, and
a disabled user's or a revoked token's session ran on. ``resolve_run_principal`` (``primer/worker/executor_builders.py``) is now the single place a
run's rank is computed: the creator's role today, never above the recorded one (a promotion does not raise an old session), and a run whose creator
is deleted, disabled or revoked is refused outright, not run at a lower rank.

The resume tests drive the real ``WorkerPool`` resume branch with the real ``build_agent_executor`` and the real ``ToolExecutionManager``; only the
model, the workspace and the injection of the resumed reply are stand-ins.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.agent import Agent, AgentModel
from primer.model.api_token import ApiToken
from primer.model.chat import Message, ToolCallPart, ToolCallResult, ToolExample, ToolResultPart
from primer.model.except_ import NotFoundError
from primer.model.model_profile import ModelProfileConfig
from primer.model.principal import PrincipalRef
from primer.model.scheduler import WorkerConfig
from primer.model.user import User
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded
from primer.model_profile import ResolvedModel
from primer.toolset._describe import make_tool
from primer.toolset.internal import InternalToolsetProvider
from primer.worker.executor_builders import RunRefused, resolve_run_principal
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState
from tests.conftest import _FakeStorageProvider

NOW = datetime.now(UTC)
ADMIN_REF = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local")
USER_REF = PrincipalRef(type="user", id="u-bob", display="bob", role="user", source="local")


async def _user(sp, uid: str, role: str, *, disabled: bool = False) -> None:
    await sp.get_storage(User).create(User(id=uid, username=uid, created_at=NOW, role=role, disabled=disabled))


def _session(initiated_by: PrincipalRef | None, **fields: Any) -> WorkspaceSession:
    return WorkspaceSession(
        id=fields.pop("sid", "ses-1"), workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"),
        status=fields.pop("status", SessionStatus.WAITING), created_at=NOW, initiated_by=initiated_by, **fields,
    )


# ---- the rule --------------------------------------------------------------------------------------------------------------------------


async def test_a_demoted_admins_session_runs_at_the_role_they_hold_now():
    sp = _FakeStorageProvider()
    await _user(sp, "u-ada", "user")
    resolved = await resolve_run_principal(sp, _session(ADMIN_REF))
    assert resolved == ADMIN_REF.model_copy(update={"role": "user"}), "the recorded admin rank still granted admin"


async def test_a_promotion_never_raises_a_session_created_before_it():
    """The recorded role is a ceiling, as for a trigger's owners: a run a trigger capped at ``user`` must not rise to its owner's admin role."""
    sp = _FakeStorageProvider()
    await _user(sp, "u-bob", "admin")
    assert await resolve_run_principal(sp, _session(USER_REF)) == USER_REF


async def test_a_creator_who_still_holds_the_role_runs_unchanged():
    sp = _FakeStorageProvider()
    await _user(sp, "u-ada", "admin")
    assert await resolve_run_principal(sp, _session(ADMIN_REF)) == ADMIN_REF


@pytest.mark.parametrize("disabled", [True, False], ids=["disabled", "deleted"])
async def test_a_run_whose_creator_is_disabled_or_deleted_is_refused(disabled):
    sp = _FakeStorageProvider()
    if disabled:
        await _user(sp, "u-ada", "admin", disabled=True)
    with pytest.raises(RunRefused, match="does not run"):
        await resolve_run_principal(sp, _session(ADMIN_REF))


@pytest.mark.parametrize(
    ("token_fields", "refused"),
    [
        pytest.param({}, False, id="a live token"),
        pytest.param({"revoked_at": NOW - timedelta(minutes=1)}, True, id="revoked"),
        pytest.param({"expires_at": NOW - timedelta(minutes=1)}, True, id="expired"),
    ],
)
async def test_an_api_token_run_follows_its_token_and_its_owner(token_fields, refused):
    sp = _FakeStorageProvider()
    await _user(sp, "u-ada", "user")  # the token's owner, demoted since the token started the session
    await sp.get_storage(ApiToken).create(ApiToken(
        id="tok-1", user_id="u-ada", name="ci", token_hash="0" * 64, prefix="prmr_abc", created_at=NOW, **token_fields,
    ))
    token_ref = PrincipalRef(type="api_token", id="tok-1", display="ci", role="admin", source="internal")
    if refused:
        with pytest.raises(RunRefused):
            await resolve_run_principal(sp, _session(token_ref))
        return
    assert await resolve_run_principal(sp, _session(token_ref)) == token_ref.model_copy(update={"role": "user"})


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param(None, id="no recorded creator"),
        pytest.param(PrincipalRef.unattributed(), id="unattributed"),
        pytest.param(PrincipalRef.system(), id="system"),
        pytest.param(PrincipalRef(type="trigger", id="tr-1", display="tr-1", role="user", source="internal"), id="a legacy trigger run"),
    ],
)
async def test_a_principal_that_names_no_account_is_kept_as_recorded(ref):
    """Nothing to look up: these name no user or token, and every one of them already ranks as recorded (system, or an ordinary user)."""
    resolved = await resolve_run_principal(_FakeStorageProvider(), _session(ref))
    assert resolved == (ref or PrincipalRef.unattributed())


# ---- the real executor build, and the real approval resume -----------------------------------------------------------------------------


class _AgentSession:
    """Just enough AgentSession for the builder (the executor reads the fragment and the ids)."""

    def __init__(self, sid: str) -> None:
        self.session_id = sid
        self.workspace_id = "ws-1"
        self.agent_id = "ag-1"
        self.workspace_tools: list = []
        self.system_prompt_fragment = "[workspace]"


class _Workspace:
    id = "ws-1"

    def __init__(self, sid: str) -> None:
        self._session = _AgentSession(sid)
        self.lines: list[bytes] = []

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        self.lines.append(line)

    async def get_session(self, session_id):
        return self._session if session_id == self._session.session_id else None

    async def file_info(self, path: str):
        raise NotFoundError(f"{path!r} not found")

    async def read_file(self, path: str) -> bytes:
        raise NotFoundError(f"{path!r} not found")


def _admin_toolset(calls: list) -> InternalToolsetProvider:
    """A toolset with one admin-only tool that records every run of it."""

    async def _nuke(arguments: dict) -> ToolCallResult:
        calls.append(arguments)
        return ToolCallResult(output=json.dumps({"nuked": True}), is_error=False)

    tool = make_tool(
        id="nuke", toolset_id="adm", purpose="Delete everything.", when="Use when an admin asks for it; never otherwise.",
        args_schema={"type": "object", "properties": {}}, examples=[ToolExample(args={})], required_role="admin",
    )
    return InternalToolsetProvider("adm", {"nuke": (tool, _nuke)})


class _Registry:
    def __init__(self, provider) -> None:
        self._provider = provider

    async def get_toolset(self, toolset_id: str):
        return self._provider if toolset_id == "adm" else None


async def _pool(monkeypatch, sp, calls: list) -> WorkerPool:
    sessions = sp.get_storage(WorkspaceSession)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,  # type: ignore[arg-type]
        storage=sp,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=_Registry(_admin_toolset(calls)),  # type: ignore[arg-type]
        engine=InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sessions)}),
    )
    pool._worker_id = "wrk-1"

    async def _resolve_llm(_agent, _override=None):
        return object(), ResolvedModel(
            profile_id="prov-1--m-1", provider_id="prov-1", model_name="m-1", context_length=8000, config=ModelProfileConfig(),
        )

    monkeypatch.setattr(pool, "_resolve_llm", _resolve_llm)
    await sp.get_storage(Agent).create(Agent(
        id="ag-1", description="an operator", model=AgentModel(profile_id="prov-1--m-1"), tools=["adm__nuke"], system_prompt=["sys"],
    ))
    return pool


async def test_a_demoted_admins_next_turn_cannot_run_an_admin_tool(monkeypatch):
    sp = _FakeStorageProvider()
    calls: list = []
    pool = await _pool(monkeypatch, sp, calls)
    await _user(sp, "u-ada", "user")
    driver = await pool._build_agent_executor(_session(ADMIN_REF), _Workspace("ses-1"))
    manager = driver._executor._tool_manager

    result = await manager.execute(ToolCallPart(id="c1", name="adm__nuke", arguments={}))

    assert result.error is True and "requires the 'admin' role" in str(result.output)
    assert calls == []


def _parked_on_an_approval(sid: str) -> WorkspaceSession:
    """An admin's session parked on the approval of its own ``adm__nuke`` call; the operator has answered ``approved``."""
    parked_at = NOW - timedelta(seconds=5)
    call = {"id": "tc-1", "name": "adm__nuke", "arguments": {}}
    parked = ParkedState(
        yielded=Yielded(
            tool_name="_approval", event_key=f"tool_approval:{sid}:tc-1", timeout=600.0,
            resume_metadata={
                "parked_at_iso": parked_at.isoformat(), "policy_id": "p1", "approval_type": "required", "gate_reason": "always-on",
                "original_call": call,
            },
        ),
        llm_messages=[Message(role="assistant", parts=[ToolCallPart(**call)]).model_dump(mode="json")],
        turn_no=0,
        started_at=parked_at,
        tool_call_id="tc-1",
        resume_event_payload={"decision": "approved"},
    )
    return _session(
        ADMIN_REF, sid=sid, status=SessionStatus.RUNNING, turn_no=0, parked_status="resumable", parked_event_key=f"tool_approval:{sid}:tc-1",
        parked_until=parked_at + timedelta(seconds=600), parked_at=parked_at, parked_state=parked.to_jsonable(),
    )


async def _resume(monkeypatch, sp, pool, sid: str) -> list[ToolResultPart]:
    """Claim the parked session and run the real resume branch; return the tool result it would inject into the turn."""
    injected: list[ToolResultPart] = []

    async def _load(_ws_id):
        return _Workspace(sid)

    async def _inject(session, executor, parked, tool_result_part):
        injected.append(tool_result_part)
        return ReleaseOutcome(success=True, drop_lease=True)

    monkeypatch.setattr(pool, "_load_workspace_for_persist", _load)
    monkeypatch.setattr(pool, "_inject_resume_and_continue", _inject)
    await pool._engine.mark_resumable(ClaimKind.SESSION, sid)
    leases = await pool._engine.claim_due("wrk-1", max_count=10)
    lease = next(ln for ln in leases if ln.kind == ClaimKind.SESSION and ln.entity_id == sid)
    await pool._run_engine_session(lease)
    return injected


async def test_an_admin_tool_call_approved_after_the_admin_was_demoted_does_not_run_as_admin(monkeypatch):
    """G6-6's scenario: the call was parked while its creator was an admin and is approved after the demotion. The re-run is ranked now."""
    sp = _FakeStorageProvider()
    calls: list = []
    pool = await _pool(monkeypatch, sp, calls)
    await _user(sp, "u-ada", "user")
    await sp.get_storage(WorkspaceSession).create(_parked_on_an_approval("ses-1"))

    injected = await _resume(monkeypatch, sp, pool, "ses-1")

    assert calls == [], "the approved admin tool ran for a demoted admin"
    (result,) = injected
    assert result.id == "tc-1" and result.error is True and "requires the 'admin' role" in str(result.output)


async def test_an_admin_tool_call_approved_while_the_admin_still_is_one_runs(monkeypatch):
    """The control: the same resume with the creator still an admin runs the tool, so the refusal above is the demotion's."""
    sp = _FakeStorageProvider()
    calls: list = []
    pool = await _pool(monkeypatch, sp, calls)
    await _user(sp, "u-ada", "admin")
    await sp.get_storage(WorkspaceSession).create(_parked_on_an_approval("ses-1"))

    injected = await _resume(monkeypatch, sp, pool, "ses-1")

    assert calls == [{}]
    (result,) = injected
    assert result.error is False and json.loads(result.output) == {"nuked": True}


async def test_an_admin_tool_call_approved_after_the_user_was_disabled_is_refused(monkeypatch, caplog):
    """A disabled creator's run is refused outright: the call does not run at any rank, nothing is injected, and the session ends failed instead of
    staying parked with no lease. The transcript and the row say why in fixed words, and the log has a warning, not a traceback (review of #853,
    N5)."""
    from primer.authz import RUN_REFUSED_MESSAGE

    sp = _FakeStorageProvider()
    calls: list = []
    pool = await _pool(monkeypatch, sp, calls)
    workspace = _Workspace("ses-1")

    class _Registry:
        async def get_workspace(self, _wid):
            return workspace

        async def check_workspace_allowed(self, _wid):
            return None

    pool._workspace_registry = _Registry()
    await _user(sp, "u-ada", "admin", disabled=True)
    await sp.get_storage(WorkspaceSession).create(_parked_on_an_approval("ses-1"))

    with caplog.at_level("WARNING"):
        injected = await _resume(monkeypatch, sp, pool, "ses-1")

    assert calls == [] and injected == []
    row = await sp.get_storage(WorkspaceSession).get("ses-1")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.last_turn_error is not None and row.last_turn_error.code == "run_refused"
    records = [json.loads(line) for blob in workspace.lines for line in blob.decode().splitlines() if line.strip()]
    assert [(r["kind"], r["payload"]) for r in records if r["kind"] == "error"] == [
        ("error", {"message": RUN_REFUSED_MESSAGE, "code": "run_refused"}),
    ]
    refused = [r for r in caplog.records if "does not run" in r.getMessage()]
    assert refused and all(r.levelname == "WARNING" and r.exc_info is None for r in refused)
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")], "a refused run is not a fault"


async def test_a_graph_resume_whose_creator_is_gone_ends_with_the_same_record(monkeypatch, caplog):
    """The graph resume ends its graph through ``end_graph`` (its end record) and says why in the same fixed words (review of #853, N5)."""
    from primer.authz import RUN_REFUSED_MESSAGE, RunRefused
    from primer.model.workspace_session import GraphSessionBinding
    from primer.worker.graph_resume_coordinator import resume_graph_engine

    sp = _FakeStorageProvider()
    workspace = _Workspace("ses-g")

    class _Registry:
        async def get_workspace(self, _wid):
            return workspace

    pool = await _pool(monkeypatch, sp, [])
    pool._workspace_registry = _Registry()

    async def _load(_ws_id):
        return workspace

    async def _refused(_session, _workspace):
        raise RunRefused("session 'ses-g' does not run: the account or API token that started it is deleted, disabled, revoked or expired")

    monkeypatch.setattr(pool, "_load_workspace_for_persist", _load)
    monkeypatch.setattr(pool, "_build_graph_executor", _refused)
    parked_at = NOW - timedelta(seconds=5)
    parked = ParkedState(
        yielded=Yielded(tool_name="_approval", event_key="tool_approval:ses-g:tc-1", timeout=600.0, resume_metadata={}),
        llm_messages=[], turn_no=0, started_at=parked_at, tool_call_id="tc-1", resume_event_payload={"decision": "approved"},
        graph_checkpoint={},
    )
    session = WorkspaceSession(
        id="ses-g", workspace_id="ws-1", binding=GraphSessionBinding(graph_id="g-1"), status=SessionStatus.RUNNING, created_at=NOW,
        initiated_by=ADMIN_REF, parked_status="resumable", parked_at=parked_at, parked_state=parked.to_jsonable(),
    )
    await sp.get_storage(WorkspaceSession).create(session)

    with caplog.at_level("WARNING"):
        await resume_graph_engine(pool, session, parked)

    row = await sp.get_storage(WorkspaceSession).get("ses-g")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.last_turn_error is not None and row.last_turn_error.code == "run_refused"
    records = [json.loads(line) for blob in workspace.lines for line in blob.decode().splitlines() if line.strip()]
    assert {"message": RUN_REFUSED_MESSAGE, "code": "run_refused"} in [r["payload"] for r in records if r["kind"] == "error"]
    refused = [r for r in caplog.records if "does not run" in r.getMessage()]
    assert refused and all(r.levelname == "WARNING" and r.exc_info is None for r in refused)
