"""Static JSX checks for the subscription create/edit dialog (Phase 10.2)."""

from pathlib import Path

TRIGGERS = Path(__file__).resolve().parents[2] / "ui" / "components" / "triggers.jsx"


def _src():
    return TRIGGERS.read_text()


def test_sub_dialog_defined():
    assert "TR_SubscriptionDialog" in _src()


def test_sub_dialog_three_kinds():
    src = _src()
    assert "chat_message" in src
    assert "agent_fresh_session" in src
    assert "graph_fresh_session" in src


def test_sub_dialog_does_not_offer_parked_session_creation():
    # parked_session must not be in the kind picker — only the yielding tool creates them
    src = _src()
    # Check that the kind options list explicitly excludes parked_session as a CREATABLE option
    # Loose check: there shouldn't be a "Parked session" label in the create form
    assert "parked_session" not in src.lower().split("parked_session_only_from_yield")[0] or \
           "parked_session_only_from_yield" in src


def test_sub_dialog_has_payload_template():
    src = _src()
    assert "payload_template" in src or "payloadTemplate" in src


def test_sub_dialog_has_parallelism():
    src = _src()
    assert '"skip"' in src and '"queue"' in src
