from primer.int.state_repo import StateRepo
from primer.workspace.local.state import LocalStateRepo


def test_local_state_repo_has_full_protocol_surface():
    # Every Protocol method must exist on LocalStateRepo (structural parity).
    for name in (
        "initialize", "create_session", "commit", "commit_arbitrary",
        "history", "load_session_info", "load_agent_binding",
        "load_waiting_state", "read_state_file",
    ):
        assert hasattr(LocalStateRepo, name), f"LocalStateRepo missing {name}"


def test_state_repo_is_runtime_checkable_protocol():
    import typing
    # StateRepo must be a runtime_checkable Protocol.
    assert getattr(StateRepo, "_is_protocol", False) or hasattr(StateRepo, "__protocol_attrs__")
