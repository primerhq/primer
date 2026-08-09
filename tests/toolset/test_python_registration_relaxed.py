"""register_module relaxations for service bundles (spec decisions 7-8).

Service functions never land in LLM context, so the docstring anatomy
bar buys nothing there; annotations stay required because they build
the schema. Yielding is rejected for service bundles because the
gateway is synchronous request/response.
"""

import pytest

from primer.toolset.python_runner.registration import (
    RegistrationError,
    register_module,
)

BARE = """
@primer_tool(timeout_seconds=5)
async def add(a: int, b: int) -> int:
    return a + b
"""

PARTIAL_DOC = '''
@primer_tool()
async def greet(name: str) -> str:
    """Greet someone."""
    return "hi " + name
'''

YIELDING = '''
@primer_tool()
async def ask(question: str) -> str:
    """Ask.

    Use when asking.

    Args:
        question: What to ask.
    """
    return ask_user(question)


@resumes(ask)
def _(payload: dict, meta: dict) -> str:
    """Resume."""
    return str(payload)
'''


def test_default_still_requires_docstrings() -> None:
    with pytest.raises(RegistrationError):
        register_module(BARE, "ts", 30.0)


def test_relaxed_accepts_bare_function() -> None:
    tools = register_module(BARE, "ts", 30.0, require_docstrings=False)
    assert len(tools) == 1
    assert tools[0].timeout_seconds == 5.0
    assert tools[0].fn_name == "add"
    assert set(tools[0].tool.args_schema["properties"]) == {"a", "b"}


def test_relaxed_accepts_partial_docstring() -> None:
    # Summary present but no Args section: strict mode rejects the
    # undocumented parameter, relaxed mode accepts it.
    with pytest.raises(RegistrationError):
        register_module(PARTIAL_DOC, "ts", 30.0)
    tools = register_module(PARTIAL_DOC, "ts", 30.0, require_docstrings=False)
    assert set(tools[0].tool.args_schema["properties"]) == {"name"}


def test_relaxed_still_requires_annotations() -> None:
    src = "@primer_tool()\nasync def f(x) -> int:\n    return 1\n"
    with pytest.raises(RegistrationError):
        register_module(src, "ts", 30.0, require_docstrings=False)


def test_yielding_rejected_when_disallowed() -> None:
    with pytest.raises(RegistrationError) as ei:
        register_module(YIELDING, "ts", 30.0, allow_yielding=False)
    assert "ask" in str(ei.value)


def test_yielding_still_allowed_by_default() -> None:
    tools = register_module(YIELDING, "ts", 30.0)
    assert len(tools) == 1
    assert tools[0].resume_fn_name is not None
