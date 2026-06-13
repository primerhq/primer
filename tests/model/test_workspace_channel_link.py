from primer.model.workspace import Workspace, WorkspaceChannelLink

_RUNTIME_META = {"url": "ws://localhost:5959", "token": "tok"}


def test_workspace_channel_link_roundtrip():
    ws = Workspace.model_validate({
        "id": "ws-1", "template_id": "t", "provider_id": "p",
        "created_at": "2026-06-13T00:00:00Z",
        "runtime_meta": _RUNTIME_META,
        "channel_association": {"channel_id": "ch-1"}})
    assert ws.channel_association.channel_id == "ch-1"


def test_channel_association_optional():
    ws = Workspace.model_validate({
        "id": "ws-1", "template_id": "t", "provider_id": "p",
        "created_at": "2026-06-13T00:00:00Z",
        "runtime_meta": _RUNTIME_META})
    assert ws.channel_association is None
