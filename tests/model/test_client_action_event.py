"""_ClientAction: the synthetic delivery event for a notifying call."""

from __future__ import annotations

from pydantic import TypeAdapter

from primer.model.chat import ExtendedEvent, StreamEvent, _ClientAction


def test_client_action_round_trips_through_the_stream_union() -> None:
    ev = ExtendedEvent(
        extended=_ClientAction(
            call_id="tc-1",
            name="client__open_file",
            arguments={"path": "a.txt", "line": 3},
        )
    )
    dumped = ev.model_dump(mode="json")
    assert dumped["extended"]["type"] == "client_action"
    back = TypeAdapter(StreamEvent).validate_python(dumped)
    assert isinstance(back, ExtendedEvent)
    assert isinstance(back.extended, _ClientAction)
    assert back.extended.call_id == "tc-1"
    assert back.extended.name == "client__open_file"
    assert back.extended.arguments == {"path": "a.txt", "line": 3}


def test_arguments_default_to_empty() -> None:
    a = _ClientAction(call_id="tc-2", name="misc__inform_user")
    assert a.arguments == {}


def test_agent_events_reexports_it() -> None:
    from primer.agent.events import _ClientAction as reexported

    assert reexported is _ClientAction
