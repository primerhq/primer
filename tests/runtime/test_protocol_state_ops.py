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


def test_protocol_version_bumped_to_1_4_in_both_copies_and_the_server():
    # The PROTOCOL_VERSION constant must read "1.4" in both protocol copies AND in the server's own constant (the hello
    # response reports the server's: it is what a client's capability check, such as exec_cancel, reads). 1.4 added the
    # exec_cancel op; same major as 1.3, so a 1.3 peer stays compatible (a client only sends exec_cancel to a runtime
    # that reported >= 1.4).
    from primer_runtime import server

    assert PROTOCOL_VERSION == "1.4"
    assert rt_protocol.PROTOCOL_VERSION == "1.4"
    assert server.PROTOCOL_VERSION == "1.4"


def test_exec_cancel_op_present_in_both_copies():
    assert OpName.EXEC_CANCEL == "exec_cancel"
    assert rt_protocol.OpName.EXEC_CANCEL == "exec_cancel"
