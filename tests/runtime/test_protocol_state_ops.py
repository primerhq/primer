from primer.workspace.runtime.protocol import OpName, PROTOCOL_VERSION
import primer_runtime.protocol as rt_protocol


def test_state_op_names_present_platform():
    assert OpName.STATE_COMMIT == "state_commit"
    assert OpName.STATE_READ == "state_read"
    assert OpName.STATE_HISTORY == "state_history"


def test_state_op_names_present_runtime():
    assert rt_protocol.OpName.STATE_COMMIT == "state_commit"
    assert rt_protocol.OpName.STATE_READ == "state_read"
    assert rt_protocol.OpName.STATE_HISTORY == "state_history"


def test_protocol_version_bumped_to_1_3_both_copies():
    # The PROTOCOL_VERSION constant must read "1.3" in both protocol copies
    # (bumped for the events_subscribe broadcast op; same major as 1.2, so
    # a 1.2 peer stays compatible).
    assert PROTOCOL_VERSION == "1.3"
    assert rt_protocol.PROTOCOL_VERSION == "1.3"
