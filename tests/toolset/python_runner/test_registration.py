"""AST registration: source in, Tool descriptors out. Never executes the module."""

from __future__ import annotations

import pytest

from primer.toolset.python_runner.registration import (
    RegistrationError,
    register_module,
)

SRC = '''
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return f"hello {name}"


@primer_tool(timeout_seconds=60)
async def ask_the_operator(question: str, ctx) -> str:
    """Ask the operator a question and wait for a reply.

    Use when a decision needs a human.

    Args:
        question: What to ask.
    """
    return ask_user(question)


@resumes(ask_the_operator)
def _ask_resume(payload: dict, meta: dict) -> str:
    """Return the operator's answer.

    Use when resuming the ask.

    Args:
        payload: The response payload.
        meta: The resume metadata.
    """
    return payload["response"]
'''


def _by_id(src: str = SRC):
    return {t.tool.id: t for t in register_module(src, "ts-1", 30.0)}


def test_only_decorated_functions_become_tools() -> None:
    assert register_module("def helper(): pass", "ts-1", 30.0) == []


def test_a_decorated_function_becomes_a_tool() -> None:
    tools = _by_id()
    assert "greet" in tools
    assert tools["greet"].tool.toolset_id == "ts-1"
    assert "Greet a person by name." in tools["greet"].tool.description


def test_the_schema_reaches_the_tool() -> None:
    tools = _by_id()
    assert tools["greet"].tool.args_schema["properties"]["name"]["type"] == "string"


def test_a_resumes_companion_marks_the_tool_yielding() -> None:
    tools = _by_id()
    assert tools["ask_the_operator"].tool.yields is True
    assert tools["ask_the_operator"].resume_fn_name == "_ask_resume"
    assert tools["greet"].tool.yields is False


def test_the_resume_companion_is_not_itself_a_tool() -> None:
    # It has no @primer_tool, so it must not reach the LLM's tool list.
    assert "_ask_resume" not in _by_id()


def test_per_tool_timeout_overrides_the_default() -> None:
    tools = _by_id()
    assert tools["ask_the_operator"].timeout_seconds == 60
    assert tools["greet"].timeout_seconds == 30.0


def test_a_timeout_above_the_ceiling_is_rejected() -> None:
    src = (
        "@primer_tool(timeout_seconds=301)\n"
        "def f(a: str) -> str:\n"
        '    """Do it.\n\n    Use when you must.\n\n    Args:\n        a: A.\n    """\n'
    )
    with pytest.raises(RegistrationError) as exc:
        register_module(src, "ts-1", 30.0)
    assert exc.value.field == "timeout_seconds"


def test_a_syntax_error_carries_a_line_number() -> None:
    with pytest.raises(RegistrationError) as exc:
        register_module("def f(:\n", "ts-1", 30.0)
    assert exc.value.lineno is not None


def test_a_missing_docstring_is_reported_against_the_function() -> None:
    with pytest.raises(RegistrationError) as exc:
        register_module(
            "@primer_tool()\ndef f(a: str) -> str:\n    return a\n", "ts-1", 30.0
        )
    assert "f" in str(exc.value)
    assert exc.value.lineno is not None


def test_a_resumes_pointing_at_an_unknown_function_is_an_error() -> None:
    src = (
        "@resumes(nope)\n"
        "def r(payload: dict, meta: dict) -> str:\n"
        '    """Resume.\n\n    Use when resuming.\n\n'
        '    Args:\n        payload: P.\n        meta: M.\n    """\n'
    )
    with pytest.raises(RegistrationError):
        register_module(src, "ts-1", 30.0)


def test_registration_never_executes_the_module() -> None:
    # A module that raises at import time must still register cleanly: the
    # source is untrusted, so the host reads it structurally and never runs it.
    src = (
        "raise RuntimeError('module executed')\n"
        "@primer_tool()\n"
        "def f(a: str) -> str:\n"
        '    """Do it.\n\n    Use when you must.\n\n    Args:\n        a: A.\n    """\n'
    )
    assert [t.tool.id for t in register_module(src, "ts-1", 30.0)] == ["f"]


def test_a_bare_decorator_without_parens_is_accepted() -> None:
    src = (
        "@primer_tool\n"
        "def f(a: str) -> str:\n"
        '    """Do it.\n\n    Use when you must.\n\n    Args:\n        a: A.\n    """\n'
    )
    assert [t.tool.id for t in register_module(src, "ts-1", 30.0)] == ["f"]


def test_docstring_examples_reach_the_tool() -> None:
    src = (
        "@primer_tool()\n"
        "def f(a: str) -> str:\n"
        '    """Do it.\n\n    Use when you must.\n\n    Args:\n        a: A.\n\n'
        '    Examples:\n        {"a": "x"}\n    """\n'
    )
    tool = register_module(src, "ts-1", 30.0)[0].tool
    assert tool.examples[0].args == {"a": "x"}


def test_an_example_that_violates_the_schema_is_rejected() -> None:
    # make_tool validates examples against the schema; a wrong example in a
    # docstring is a registration failure, not a silently shipped lie.
    src = (
        "@primer_tool()\n"
        "def f(a: str) -> str:\n"
        '    """Do it.\n\n    Use when you must.\n\n    Args:\n        a: A.\n\n'
        '    Examples:\n        {"b": "x"}\n    """\n'
    )
    with pytest.raises(RegistrationError):
        register_module(src, "ts-1", 30.0)
