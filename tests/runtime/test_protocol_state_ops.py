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


def test_protocol_version_bumped_to_1_2_both_copies():
    # The PROTOCOL_VERSION constant must read "1.2" in both protocol copies
    # (bumped for the optional exec access/writes fields; same major as 1.1,
    # so a 1.1 peer stays compatible).
    assert PROTOCOL_VERSION == "1.2"
    assert rt_protocol.PROTOCOL_VERSION == "1.2"
