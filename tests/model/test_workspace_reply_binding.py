from primer.model.workspace import Workspace, WorkspaceChannelLink

_RUNTIME_META = {"url": "ws://localhost:5959", "token": "tok"}


def test_reply_binding_roundtrip():
    ws = Workspace.model_validate({
        "id": "ws-1", "template_id": "t", "provider_id": "p",
        "created_at": "2026-06-13T00:00:00Z",
        "runtime_meta": _RUNTIME_META,
        "reply_binding": {"channel_id": "ch-1"}})
    assert ws.reply_binding.channel_id == "ch-1"


def test_reply_binding_optional():
    ws = Workspace.model_validate({
        "id": "ws-1", "template_id": "t", "provider_id": "p",
        "created_at": "2026-06-13T00:00:00Z",
        "runtime_meta": _RUNTIME_META})
    assert ws.reply_binding is None


def test_legacy_channel_association_aliases_into_reply_binding():
    """A row stored under the old ``channel_association`` key loads into
    ``reply_binding`` (back-compat for persisted workspaces)."""
    ws = Workspace.model_validate({
        "id": "ws-1", "template_id": "t", "provider_id": "p",
        "created_at": "2026-06-13T00:00:00Z",
        "runtime_meta": _RUNTIME_META,
        "channel_association": {"channel_id": "ch-legacy"}})
    assert ws.reply_binding is not None
    assert ws.reply_binding.channel_id == "ch-legacy"
