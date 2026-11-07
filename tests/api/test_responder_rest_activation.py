"""Authenticated REST steer responder attribution activation."""

from types import SimpleNamespace

from primer.api.routers.workspaces import _trusted_rest_responder_input
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.api.test_session_access_routes import (  # noqa: F401
    NOW,
    SID,
    WS,
    _workspace,
    cast,
    world,
)


async def test_plain_authenticated_rest_steer_rotates_responder(
    world, app, cast,
) -> None:  # noqa: F811
    sp = app.state.storage_provider
    await sp.get_storage(Workspace).create(
        _workspace(owners=[cast.carol_id], grants={cast.dave_id: "full"}),
    )
    carol = PrincipalRef(
        type="user", id=cast.carol_id, display="carol", role="user",
        source="local", user_id=cast.carol_id,
    )
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=SID,
        workspace_id=WS,
        binding=AgentSessionBinding(agent_id="operator"),
        status=SessionStatus.CREATED,
        turn_status="idle",
        created_at=NOW,
        initiated_by=carol,
        responder_epoch="a" * 32,
        responder_seq=0,
        responder_reservation_version=1,
        responder_reservations={},
    ))

    response = await cast.dave.post(
        f"/v1/workspaces/{WS}/sessions/{SID}/steer",
        json={"instruction": "from dave"},
    )

    assert response.status_code == 200, response.text
    row = await sp.get_storage(WorkspaceSession).get(SID)
    assert row.turn_person is not None
    assert row.turn_person.user_id == cast.dave_id
    assert (row.responder_seq, row.responder_reservation_version) == (1, 2)
    assert row.responder_epoch != "a" * 32
    assert row.last_responder_intent_receipt is None


def test_only_plain_person_rest_shape_is_trusted() -> None:
    person = PrincipalRef(
        type="user", id="person-1", display="Person", role="user",
        source="local", user_id="person-1",
    )
    plain = SimpleNamespace(
        instruction="hello", tool_results=None, external_tools=None,
        attachments=None,
    )
    trusted = _trusted_rest_responder_input(plain, person)
    assert trusted is not None and trusted.person == person

    excluded = [
        SimpleNamespace(
            instruction=None, tool_results=[object()], external_tools=None,
            attachments=None,
        ),
        SimpleNamespace(
            instruction="hello", tool_results=[object()], external_tools=None,
            attachments=None,
        ),
        SimpleNamespace(
            instruction="hello", tool_results=None, external_tools=[],
            attachments=None,
        ),
        SimpleNamespace(
            instruction="hello", tool_results=None, external_tools=None,
            attachments=[object()],
        ),
    ]
    assert all(_trusted_rest_responder_input(body, person) is None for body in excluded)
    assert _trusted_rest_responder_input(plain, None) is None
    assert _trusted_rest_responder_input(plain, PrincipalRef.system()) is None
