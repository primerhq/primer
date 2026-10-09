"""Through the REAL REST route: after a NON-primary sibling of an agent session's child graph is answered, the console still judges every remaining gate by its
own approvers (C-033 round 4, from the #683 round 3 review).

The post-repark row is the one the real resume writes (``tests/worker/test_child_graph_repark_three_gates.py``'s scenario, step 1). ``repark_continuation`` used
to write no top-level ``graph_checkpoint``, so the only pending entry the routes saw was the PROJECTION of the primary, and the projection of a ToolCall primary
carries ``original_call`` / ``gate_id`` but no ``approvers`` (``primer/graph/_checkpoint.py`` ``_build_pending_park_yield``): bob passed a gate routed to alice only,
and the other tool_call gate was not decidable at all.
"""

from __future__ import annotations

import pytest

from primer.model.workspace_session import WorkspaceSession
from tests.api.conftest import raw_client as auth_client  # noqa: F401
from tests.api.test_approver_routing import _login_user, _register_admin
from tests.worker.test_child_graph_repark_three_gates import G, SID, _scenario


@pytest.mark.asyncio
async def test_bob_cannot_decide_the_alice_only_primary_after_a_sibling_answer(auth_client, app, monkeypatch):  # noqa: F811
    trace = await _scenario(monkeypatch, {"T1": {"kind": "users", "users": ["alice"]}, "T2": {"kind": "users", "users": ["alice"]}})
    row = trace["_reparked_row"]
    primary = trace["primary"]
    await _register_admin(auth_client)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(row.model_copy(update={"parked_state": dict(row.parked_state)}))
    await _login_user(auth_client, app, "bob")

    resp = await auth_client.post(f"/v1/sessions/{SID}/tool_approval/respond",
                                  json={"tool_call_id": f"u-{primary}", "gate_id": G[primary], "decision": "approved"})
    after = await storage.get(SID)
    assert after.parked_status == "parked", "a refused decision flips nothing"
    assert resp.status_code == 403, f"bob decided {primary}, which is routed to alice only: {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_the_other_tool_call_gate_is_not_decidable_in_the_console_after_a_sibling_answer(auth_client, app, monkeypatch):  # noqa: F811
    trace = await _scenario(monkeypatch, {})
    row = trace["_reparked_row"]
    other = trace["clicked"]
    await _register_admin(auth_client)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(row.model_copy(update={"parked_state": dict(row.parked_state)}))
    await _login_user(auth_client, app, "alice")
    resp = await auth_client.post(f"/v1/sessions/{SID}/tool_approval/respond",
                                  json={"tool_call_id": f"u-{other}", "gate_id": G[other], "decision": "approved"})
    assert resp.status_code == 202, f"{other} is still pending in the child but the console cannot decide it: {resp.status_code}"
