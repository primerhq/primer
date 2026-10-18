"""Where a refusal of the agent form goes (platform revamp plan 05, Task 3; Review Focus 5: a refusal the operator cannot see).

A save the server answers with 422 carries one message per field path (``body.temperature``, ``body.system_prompt.1``). The form keeps them in a map under those
paths. Until now the form showed a message only for the fields it had a place for, and stored any other under a key no field read, so the save did nothing and
said nothing. Three pure helpers of ``agents.jsx`` (the overlay of Task 4 uses them) put every message somewhere:

* ``AG_FIELD_PATHS``: the paths the overlay shows a field for;
* ``AG_fieldError(map, path)``: the message for one field (its own, or the first under it: ``body.system_prompt.1`` is the system prompt's);
* ``AG_otherFieldErrors(map, paths)``: one line for every message that no field in ``paths`` owns, for the line above the footer.

They run here in V8, on the real source.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._agent_form_harness import agent_form


@pytest.fixture(scope="module")
def ctx():
    with agent_form() as c:
        yield c


def _call(ctx, expression: str):
    """The value of a JS expression, as JSON; ``None`` for ``undefined`` (JSON drops the key)."""
    return json.loads(ctx.eval(f"JSON.stringify({{v: {expression}}})")).get("v")


def field_error(ctx, errors: dict, path: str):
    return _call(ctx, f"AG_fieldError({json.dumps(errors)}, {json.dumps(path)})")


def other_errors(ctx, errors: dict):
    return _call(ctx, f"AG_otherFieldErrors({json.dumps(errors)}, AG_FIELD_PATHS)")


def field_paths(ctx):
    return _call(ctx, "AG_FIELD_PATHS")


def test_a_message_at_the_path_is_returned(ctx):
    message = "Input should be greater than or equal to 0"
    assert field_error(ctx, {"body.temperature": message}, "body.temperature") == message


def test_a_message_under_the_path_is_returned(ctx):
    assert field_error(ctx, {"body.system_prompt.1": "String should have at most 100000 characters"}, "body.system_prompt") == (
        "String should have at most 100000 characters"
    )
    assert field_error(ctx, {"body.model.profile_id": "String should have at least 1 character"}, "body.model") == (
        "String should have at least 1 character"
    )


def test_a_key_that_only_starts_with_the_same_letters_is_not_under_the_path(ctx):
    assert field_error(ctx, {"body.tools_extra": "x"}, "body.tools") is None


def test_a_cleared_message_is_no_message(ctx):
    assert field_error(ctx, {"body.id": None}, "body.id") is None


def test_messages_no_field_shows_become_lines(ctx):
    errors = {"body.id": "a", "body.harness_id": "Extra inputs are not permitted", "body.system_prompt.0": "c", "body": "whole"}
    assert other_errors(ctx, errors) == ["harness_id: Extra inputs are not permitted", "whole"]  # paths = AG_FIELD_PATHS
    assert other_errors(ctx, {}) == []


def test_the_paths_are_the_fields_the_overlay_shows(ctx):
    assert field_paths(ctx) == [
        "body.id", "body.description", "body.model", "body.system_prompt", "body.compaction_prompt",
        "body.temperature", "body.max_tool_turns", "body.max_output_tokens", "body.tools",
        "body.tts_voice", "body.response_format",
    ]


def test_a_key_outside_the_body_keeps_its_whole_path_in_the_line(ctx):
    assert other_errors(ctx, {"query.limit": "bad"}) == ["query.limit: bad"]


def test_empty_messages_make_no_line(ctx):
    assert other_errors(ctx, {"body.harness_id": "", "body.extra": None}) == []


def test_the_caller_names_the_paths_it_shows(ctx):
    """A voice control that is not drawn (text to speech off) owns no path: its refusal is a line, not a message nobody sees."""
    shown = [p for p in field_paths(ctx) if p != "body.tts_voice"]
    errors = {"body.tts_voice": "Unknown voice", "body.id": "taken"}
    assert _call(ctx, f"AG_otherFieldErrors({json.dumps(errors)}, {json.dumps(shown)})") == ["tts_voice: Unknown voice"]
