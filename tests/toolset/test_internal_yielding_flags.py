"""InternalToolsetProvider.is_yielding / requires_workspace contracts.

The MCP server endpoint (Spec §7) calls these predicates to filter
its exposable tool set. The signals must hold for the canonical
yielding tools (``sleep``, ``ask_user``, ``watch_files``,
``subscribe_to_trigger``).
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
    """``ask_user`` moved to the system toolset; still yields."""
    from primer.toolset.system import build_system_toolset

    sp = _SystemSP()
    pr = _system_registry(sp)
    provider = build_system_toolset(storage_provider=sp, provider_registry=pr)
    assert provider.is_yielding("ask_user") is True


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


async def _noop_handler(arguments):  # pragma: no cover - never dispatched here
    return ToolCallResult(output="{}", is_error=False)


def test_classification_reads_explicit_flags_not_handler_source() -> None:
    """The provider reads ``Tool.yields``.

    The handler here is a plain no-op whose source contains no
    ``Yielded`` - the OLD getsource/annotation heuristic would classify
    it as non-yielding. With the explicit flag set on the Tool, the
    provider must report it as yielding, proving classification comes
    from the flag, not from introspecting the handler.
    """
    flagged = Tool(
        id="flagged_tool",
        toolset_id="t",
        description="x",
        args_schema={"type": "object"},
        yields=True,
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
    assert provider.is_yielding("plain_tool") is False


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
    # New capability flag: default False on the ABC too.
    assert p.requires_workspace("anything") is False


def test_internal_provider_requires_workspace_reads_flag() -> None:
    """InternalToolsetProvider.requires_workspace mirrors the Tool flag."""
    ws_tool = Tool(
        id="ws_tool",
        toolset_id="t",
        description="x",
        args_schema={"type": "object"},
        requires_workspace=True,
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
            "ws_tool": (ws_tool, _noop_handler),
            "plain_tool": (plain, _noop_handler),
        },
    )
    assert provider.requires_workspace("ws_tool") is True
    assert provider.requires_workspace("plain_tool") is False
    # Unknown names fail to False.
    assert provider.requires_workspace("does_not_exist") is False


def test_system_read_doc_content_requires_workspace() -> None:
    """build_system_toolset registers read_doc_content flagged workspace-only."""
    from primer.toolset.system import build_system_toolset

    sp = _SystemSP()
    pr = _system_registry(sp)

    class _Reg:  # minimal WorkspaceRegistry stand-in (never dispatched here)
        async def get_workspace(self, workspace_id):  # pragma: no cover
            return None

    provider = build_system_toolset(
        storage_provider=sp, provider_registry=pr, workspace_registry=_Reg()
    )
    assert provider.requires_workspace("read_doc_content") is True


def test_system_read_doc_content_absent_without_registry() -> None:
    """Without a workspace_registry the workspace-only tool is not wired."""
    from primer.toolset.system import build_system_toolset

    sp = _SystemSP()
    pr = _system_registry(sp)
    provider = build_system_toolset(storage_provider=sp, provider_registry=pr)
    # Absent -> unknown name -> False.
    assert provider.requires_workspace("read_doc_content") is False
