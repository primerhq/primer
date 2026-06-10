"""Yielded model: optional event_keys for multi-event parks."""
from primer.model.yield_ import Yielded


def test_yielded_event_keys_defaults_none_and_roundtrips():
    y = Yielded(tool_name="ask_user", event_key="ask_user:s:tc")
    assert y.event_keys is None
    blob = y.to_jsonable()
    assert blob.get("event_keys") is None
    assert Yielded.from_jsonable(blob).event_keys is None


def test_yielded_event_keys_set_roundtrips():
    y = Yielded(tool_name="_approval", event_key="k1",
                event_keys=["k1", "k2", "k3"])
    blob = y.to_jsonable()
    assert blob["event_keys"] == ["k1", "k2", "k3"]
    assert Yielded.from_jsonable(blob).event_keys == ["k1", "k2", "k3"]
