"""InternalToolsetProvider.is_yielding / requires_session contracts.

The MCP server endpoint (Spec §7) calls these two predicates to filter
its exposable tool set. The signals must hold for the canonical
yielding tools (``sleep``, ``ask_user``, ``watch_files``,
``subscribe_to_trigger``) and the session-bound workspace tool
(``watch_files`` reads ``ctx.session_id``).
"""

from __future__ import annotations

from primer.model.chat import Tool, ToolCallResult
from primer.toolset.internal import InternalToolsetProvider
from primer.toolset.misc import build_misc_toolset


def test_misc_yielding_tools_flagged() -> None:
    provider = build_misc_toolset()
    # ``sleep`` and ``ask_user`` both return ``ToolCallResult | Yielded``.
    assert provider.is_yielding("sleep") is True
    assert provider.is_yielding("ask_user") is True


def test_misc_non_yielding_tools_not_flagged() -> None:
    provider = build_misc_toolset()
    # Plain handlers — no Yielded in their return annotation.
    assert provider.is_yielding("uuid_v4") is False
    assert provider.is_yielding("get_datetime") is False
    assert provider.is_yielding("hash") is False
    assert provider.is_yielding("calculate") is False


def test_misc_session_free_tools_not_flagged() -> None:
    """Plain misc tools don't read ``ctx.session_id``.

    ``ask_user`` legitimately reads ``ctx.session_id`` (forms the
    yield event key), so it IS flagged as session-bound — that's
    fine because the yielding filter blocks it from MCP first.
    """
    provider = build_misc_toolset()
    for name in ("uuid_v4", "get_datetime", "hash", "calculate", "sleep"):
        assert provider.requires_session(name) is False


def test_misc_ask_user_flagged_as_session_bound() -> None:
    """``ask_user`` reads ``ctx.session_id`` — the introspection picks it up."""
    provider = build_misc_toolset()
    assert provider.requires_session("ask_user") is True


def test_unknown_name_defaults_false() -> None:
    provider = build_misc_toolset()
    assert provider.is_yielding("does_not_exist") is False
    assert provider.requires_session("does_not_exist") is False


async def _noop_handler(arguments):  # pragma: no cover - never dispatched here
    return ToolCallResult(output="{}", is_error=False)


def test_classification_reads_explicit_flags_not_handler_source() -> None:
    """The provider reads ``Tool.yields`` / ``Tool.requires_session``.

    The handler here is a plain no-op whose source contains neither
    ``Yielded`` nor ``ctx.session_id`` — the OLD getsource/annotation
    heuristics would classify it as non-yielding and session-free. With
    the explicit flags set on the Tool, the provider must report it as
    yielding + session-bound, proving classification comes from the
    flags, not from introspecting the handler.
    """
    flagged = Tool(
        id="flagged_tool",
        toolset_id="t",
        description="x",
        args_schema={"type": "object"},
        yields=True,
        requires_session=True,
    )
    plain = Tool(
        id="plain_tool",
        toolset_id="t",
        description="x",
        args_schema={"type": "object"},
    )
    provider = InternalToolsetProvider(
        toolset_id="t",
        registry={
            "flagged_tool": (flagged, _noop_handler),
            "plain_tool": (plain, _noop_handler),
        },
    )
    assert provider.is_yielding("flagged_tool") is True
    assert provider.requires_session("flagged_tool") is True
    assert provider.is_yielding("plain_tool") is False
    assert provider.requires_session("plain_tool") is False


def test_base_class_default_is_false() -> None:
    """A provider that doesn't override returns False for every name."""
    from primer.int.toolset import ToolsetProvider

    class _Minimal(ToolsetProvider):
        async def list_tools(self, *, principal=None):  # type: ignore[override]
            if False:  # pragma: no cover — empty async generator
                yield

        async def call(self, *, tool_name, arguments, principal=None, ctx=None):
            raise NotImplementedError

    p = _Minimal()
    assert p.is_yielding("anything") is False
    assert p.requires_session("anything") is False
