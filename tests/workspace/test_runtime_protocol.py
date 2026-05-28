from primer.workspace.runtime.protocol import (
    Request, Response, Event, ErrorCode, OpName,
    serialize, deserialize,
)


def test_op_names_complete():
    expected = {"hello", "health", "read_file", "write_file", "append_line",
                "list_dir", "stat", "delete", "archive",
                "exec", "watch_start", "watch_cancel"}
    actual = {op.value for op in OpName}
    assert actual == expected


def test_error_codes_complete():
    expected = {"ENOENT", "EACCES", "EISDIR", "ENOTDIR", "EEXIST",
                "ETIMEDOUT", "EUNSUPPORTED", "EPROTOCOL", "EINTERNAL"}
    actual = {e.value for e in ErrorCode}
    assert actual == expected


def test_round_trip_request():
    req = Request(req_id=7, op=OpName.READ_FILE, args={"path": "/tmp/x"})
    blob = serialize(req)
    parsed = deserialize(blob)
    assert parsed == req


def test_round_trip_response():
    resp = Response(req_id=7, ok=True, result={"content_b64": "..."})
    blob = serialize(resp)
    parsed = deserialize(blob)
    assert parsed == resp


def test_round_trip_event():
    evt = Event(req_id=8, event="stdout", data={"data_b64": "aGVsbG8="})
    blob = serialize(evt)
    parsed = deserialize(blob)
    assert parsed == evt


def test_round_trip_request_no_args():
    req = Request(req_id=1, op=OpName.HEALTH, args=None)
    blob = serialize(req)
    parsed = deserialize(blob)
    assert parsed == req


def test_round_trip_response_error():
    resp = Response(req_id=3, ok=False, error={"code": "ENOENT", "message": "not found"})
    blob = serialize(resp)
    parsed = deserialize(blob)
    assert parsed == resp


def test_serialize_produces_string():
    req = Request(req_id=1, op=OpName.HEALTH)
    blob = serialize(req)
    assert isinstance(blob, str)


def test_deserialize_unknown_type_raises():
    import pytest
    import json
    bad = json.dumps({"foo": "bar"})
    with pytest.raises((ValueError, KeyError)):
        deserialize(bad)
