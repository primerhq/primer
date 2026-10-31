"""PERF-T10 (M9) perf: ``list_subscriptions`` reach-checks batch the targets, one read per distinct target session and workspace, not per row.

The reach answer (a subscription whose target workspace the caller holds no full on is left out of the list, as if it did not
exist) is pinned by ``tests/toolset/test_workspace_tools_access.py::test_subscription_visibility_follows_the_callers_full_not_the_default_workspace``
and stays green through the change. The perf half is RED on the per-row reads (one session get per session-targeting row plus one
workspace read per row) and green once the distinct targets resolve in one read each.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import SecretStr

from primer.access.person import Person
from primer.model.access import Access
from primer.model.provider import SqliteConfig
from primer.model.trigger import AgentFreshSubConfig, DelayedTriggerConfig, SessionAppendSubConfig, Subscription, Trigger
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from primer.trigger.service import ServiceDeps, list_subscriptions
from tests.perf.counting import CountingStorageProvider

NOW = datetime.now(timezone.utc)
CAROL = Person("u-carol", "user")


async def _world(tmp_path: Path) -> SqliteStorageProvider:
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    now = datetime.now(timezone.utc)
    for ws_id, owner in (("ws-a", "u-carol"), ("ws-b", "u-other")):
        await sp.get_storage(Workspace).create(
            Workspace(
                id=ws_id, template_id="tpl-1", provider_id="local-1",
                runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("stub-token")),
                access=Access(owners=[owner]), created_at=now,
            )
        )
    for ses_id, ws_id in (("ses-a", "ws-a"), ("ses-b", "ws-b")):
        await sp.get_storage(WorkspaceSession).create(
            WorkspaceSession(
                id=ses_id, workspace_id=ws_id, binding=AgentSessionBinding(agent_id="ag-1"),
                status=SessionStatus.RUNNING, created_at=now,
            )
        )
    await sp.get_storage(Trigger).create(
        Trigger(
            id="tr-1", slug="one", name="One",
            config=DelayedTriggerConfig(kind="delayed", fire_at=now),
            access=Access(owners=["u-carol"]), created_at=now,
        )
    )
    subs = sp.get_storage(Subscription)
    for i in range(3):
        await subs.create(Subscription(id=f"sub-fresh-a-{i}", trigger_id="tr-1", config=AgentFreshSubConfig(workspace_id="ws-a", agent_id="ag-1"), created_at=now))
        await subs.create(Subscription(id=f"sub-fresh-b-{i}", trigger_id="tr-1", config=AgentFreshSubConfig(workspace_id="ws-b", agent_id="ag-1"), created_at=now))
        await subs.create(Subscription(id=f"sub-ses-a-{i}", trigger_id="tr-1", config=SessionAppendSubConfig(session_id="ses-a"), created_at=now))
        await subs.create(Subscription(id=f"sub-ses-b-{i}", trigger_id="tr-1", config=SessionAppendSubConfig(session_id="ses-b"), created_at=now))
    return sp


async def test_the_reach_list_of_12_subscriptions_batch_reads_the_targets(tmp_path: Path):
    sp = await _world(tmp_path)
    csp = CountingStorageProvider(sp)
    deps = ServiceDeps(storage_provider=csp)

    with csp.counted() as c:
        items = await list_subscriptions(trigger_id="tr-1", person=CAROL, reach=CAROL, deps=deps)

    # Carol holds full on ws-a: the fresh subs of ws-a and the subs feeding ses-a (in ws-a) stay; ws-b's are left out.
    assert {s.id for s in items} == {f"sub-fresh-a-{i}" for i in range(3)} | {f"sub-ses-a-{i}" for i in range(3)}
    # The two distinct target sessions and the two distinct target workspaces resolve in one read each.
    assert c.calls("get", "find", model="WorkspaceSession") <= 2
    assert c.calls("get", "find", model="Workspace") <= 2
    await sp.aclose()
