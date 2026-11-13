"""``reserved_tools()`` as the first live-catalogue touch of a fresh process.

Before the fix, the first caller in a process red: ``live_providers()`` runs its own ``asyncio.run``, and
``reserved_tools()`` called it from inside its own loop (a nested ``asyncio.run``). Run this module alone
and ``reserved_tools()`` is that first touch, so it is the pre-fix failing order: it is the proof the
resolution no longer depends on call order (``tests/docs/test_docs_name_real_tools_and_routes.py`` was the
victim - 372 failed in a fresh process before the fix).
"""
from tests.docs._live_toolsets import reserved_tools

_CURATED_TEN = {
    "system", "crud", "collections", "harness", "trigger", "workspaces",
    "workspace_ext", "web", "misc", "canvas",
}


def test_reserved_tools_resolves_when_it_is_the_first_live_call() -> None:
    """Run alone, ``reserved_tools()`` is the process's first touch of ``live_providers()`` and still
    resolves the whole reserved union: the curated ten, the generated providers and the session scope."""
    tools = reserved_tools()
    assert _CURATED_TEN <= set(tools), set(tools)
    assert "internal_collections" in tools, "the generated providers are part of the reserved union"
    assert "read" in tools["workspace"], "the session scope's tools ride the same union"
