"""A channel approval prompt draws only the arguments the approval allowlist names (01a125ed-9ef6).

The PromptEnvelope the post-park channel dispatch posts carries the SCRUBBED arguments: the stamped
allowlist (``resume_metadata["preview"]``), else the default rule for an unstamped park (design ruling
D5). A value the allowlist withholds must not reach the chat platform, in the structured ``tool_args``
or in the ``prompt`` text (which Slack posts as the fallback notification and Telegram posts when the
tool name is absent). The decision round-trip reads the original arguments from the park, never from
what was posted, so the scrubbing changes nothing downstream.
"""

from primer.worker.yield_runtime import _build_prompt_envelope

_HIDDEN = "<hidden>"


def _approval_env(arguments, preview=None):
    metadata = {
        "original_call": {"id": "tc1", "name": "write_file", "arguments": arguments},
    }
    if preview is not None:
        metadata["preview"] = preview
    return _build_prompt_envelope(
        kind="_approval", workspace_id="w", session_id="s",
        fallback_tool_call_id="tc1", metadata=metadata,
    )


def test_a_policy_stamp_withholds_every_argument_it_does_not_name() -> None:
    env = _approval_env(
        {"command": "ls -la", "api_key": "sk-live-SECRET-123"},
        preview={"paths": ["command"], "source": "policy"},
    )

    assert env is not None
    assert env.tool_args == {"command": "ls -la", "api_key": _HIDDEN}
    assert "sk-live-SECRET-123" not in env.prompt


def test_an_unstamped_park_takes_the_default_rule() -> None:
    env = _approval_env({"name": "bob", "enabled": True, "count": 2})

    assert env is not None
    assert env.tool_args == {"name": _HIDDEN, "enabled": True, "count": 2}
    assert "bob" not in env.prompt


def test_a_default_source_stamp_hides_a_value_that_is_not_of_the_closed_kind() -> None:
    # A closed-set argument (by schema) whose value is a long text string: the gate parks BEFORE schema
    # validation, so a secret sent as an "enum" does not ride on the schema's promise.
    env = _approval_env(
        {"mode": "x" * 50},
        preview={"paths": ["mode"], "source": "default"},
    )

    assert env is not None
    assert env.tool_args == {"mode": _HIDDEN}
    assert "x" * 50 not in env.prompt


def test_json_string_arguments_are_parsed_before_the_allowlist_applies() -> None:
    env = _approval_env(
        '{"command": "ls", "password": "hunter2"}',
        preview={"paths": ["command"], "source": "policy"},
    )

    assert env is not None
    assert env.tool_args == {"command": "ls", "password": _HIDDEN}
    assert "hunter2" not in env.prompt


def test_a_bare_value_no_path_can_name_is_withheld_whole() -> None:
    env = _approval_env(["sk-a", "sk-b"], preview={"paths": ["command"], "source": "policy"})

    assert env is not None
    assert env.tool_args is None
    assert "sk-a" not in env.prompt
    assert _HIDDEN in env.prompt


def test_no_arguments_still_renders_empty() -> None:
    env = _approval_env({})

    assert env is not None
    assert env.tool_args == {}
    assert "Approve write_file({})?" in env.prompt
