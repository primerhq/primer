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


def test_protocol_version_is_1_7_in_both_copies_and_the_server():
    # The PROTOCOL_VERSION constant must read "1.7" in both protocol copies AND in the server's own constant (the hello
    # response reports the server's: it is what a client's capability check, such as exec_cancel, reads). 1.4 added the
    # exec_cancel op; same major as 1.3, so a 1.3 peer stays compatible (a client only sends exec_cancel to a runtime
    # that reported >= 1.4). 1.5 added the optional read_file offset; 1.6 adds state_show_commit and history file counts. 1.7 adds bounded directory publishing.
    from primer_runtime import server

    assert PROTOCOL_VERSION == "1.7"
    assert rt_protocol.PROTOCOL_VERSION == "1.7"
    assert server.PROTOCOL_VERSION == "1.7"


def test_exec_cancel_op_present_in_both_copies():
    assert OpName.EXEC_CANCEL == "exec_cancel"
    assert rt_protocol.OpName.EXEC_CANCEL == "exec_cancel"


def test_state_show_commit_op_present_in_both_copies():
    assert OpName.STATE_SHOW_COMMIT == "state_show_commit"
    assert rt_protocol.OpName.STATE_SHOW_COMMIT == "state_show_commit"


def test_capture_implementation_is_identical_in_the_standalone_runtime():
    from pathlib import Path
    from primer.workspace import directory_bundle as platform
    from primer_runtime import directory_bundle as runtime

    assert OpName.CAPTURE_SERVICE_BUNDLE == rt_protocol.OpName.CAPTURE_SERVICE_BUNDLE
    assert Path(platform.__file__).read_bytes() == Path(runtime.__file__).read_bytes()
