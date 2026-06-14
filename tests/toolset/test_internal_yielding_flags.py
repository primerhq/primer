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


class _SP:  # minimal storage_provider stub for build_workspace_ext_toolset
    def get_storage(self, model):  # pragma: no cover - never dispatched here
        return None


def test_workspace_ext_yielding_tools_flagged() -> None:
    from primer.toolset.workspace_ext import build_workspace_ext_toolset

    provider = build_workspace_ext_toolset(storage_provider=_SP())
    # ``sleep`` moved here from misc; both return ``ToolCallResult | Yielded``.
    assert provider.is_yielding("sleep") is True
    assert provider.is_yielding("watch_files") is True
    assert provider.is_yielding("invoke_graph") is True
    assert provider.is_yielding("subscribe_to_trigger") is True


def test_system_ask_user_flagged() -> None:
    """``ask_user`` moved to the system toolset; still yields + session-bound."""
    from primer.toolset.system import build_system_toolset

    sp = _SystemSP()
    pr = _system_registry(sp)
    provider = build_system_toolset(storage_provider=sp, provider_registry=pr)
    assert provider.is_yielding("ask_user") is True
    assert provider.requires_session("ask_user") is True


def test_misc_non_yielding_tools_not_flagged() -> None:
    provider = build_misc_toolset()
    # Plain handlers — no Yielded in their return annotation.
    assert provider.is_yielding("uuid_v4") is False
    assert provider.is_yielding("get_datetime") is False
    assert provider.is_yielding("hash") is False
    assert provider.is_yielding("calculate") is False
    # sleep + ask_user no longer live in misc.
    assert provider.is_yielding("sleep") is False
    assert provider.is_yielding("ask_user") is False


def test_misc_session_free_tools_not_flagged() -> None:
    """Plain misc tools don't read ``ctx.session_id``."""
    provider = build_misc_toolset()
    for name in ("uuid_v4", "get_datetime", "hash", "calculate"):
        assert provider.requires_session(name) is False


def _system_registry(sp):
    from primer.api.registries import ProviderRegistry

    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


def _SystemSP():
    from tests.toolset.test_system import _SP as _SystemStorage

    return _SystemStorage()


def test_unknown_name_defaults_false() -> None:
    provider = build_misc_toolset()
    assert provider.is_yielding("does_not_exist") is False
    assert provider.requires_session("does_not_exist") is False


async def _noop_handler(arguments):  # pragma: no cover - never dispatched here
    return ToolCallResult(output="{}", is_error=False)


def test_classification_reads_explicit_flags_not_handler_source() -> None:
    """The provider reads ``Tool.yields`` / ``Tool.requires_session``.

    The handler here is a plain no-op whose source contains neither
    ``Yielded`` nor ``ctx.session_id`` - the OLD getsource/annotation
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
