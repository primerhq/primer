"""InternalToolsetProvider.is_yielding / requires_session contracts.

The MCP server endpoint (Spec §7) calls these two predicates to filter
its exposable tool set. The signals must hold for the canonical
yielding tools (``sleep``, ``ask_user``, ``watch_files``,
``subscribe_to_trigger``) and the session-bound workspace tool
(``watch_files`` reads ``ctx.session_id``).
"""

from __future__ import annotations

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
