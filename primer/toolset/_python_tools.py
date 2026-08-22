"""The python-toolset management tools, factored out of ``system.py``.

These three tools are the escalation path the python runner's isolation
exists to contain: a tool that registers arbitrary python is, by
construction, a tool that runs arbitrary python. The mutators are
admin-gated so a default agent cannot reach them.

Factored out of :func:`primer.toolset.system.build_system_toolset` so the
S5 ``crud`` toolset can register the same tools under its own scope
without duplicating the handlers (the misc -> workspace_ext precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from primer.model.chat import Tool, ToolExample
from primer.toolset._describe import make_tool
from primer.toolset._helpers import ok as _ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID
from primer.toolset.internal import ToolHandler

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


class _CreatePythonToolsetArgs(BaseModel):
    toolset_id: str = Field(
        ..., min_length=1, description="Id for the new python toolset."
    )
    source: str = Field(
        ...,
        min_length=1,
        description=(
            "Python module source. Every @primer_tool function in it "
            "becomes a tool."
        ),
    )
    default_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        description="Wall-clock ceiling for tools that declare none.",
    )


class _UpdatePythonToolsetSourceArgs(BaseModel):
    toolset_id: str = Field(
        ..., min_length=1, description="Id of the python toolset to edit."
    )
    source: str = Field(
        ..., min_length=1, description="Replacement module source."
    )


class _ListPythonToolsArgs(BaseModel):
    toolset_id: str = Field(
        ..., min_length=1, description="Id of the python toolset to inspect."
    )


def _derived(source: str, toolset_id: str, timeout: float) -> list[dict]:
    from primer.toolset.python_runner.registration import register_module

    return [
        {
            "id": reg.tool.id,
            "yields": reg.tool.yields,
            "timeout_seconds": reg.timeout_seconds,
            "args": sorted(reg.tool.args_schema.get("properties", {})),
        }
        for reg in register_module(source, toolset_id, timeout)
    ]


def build_python_toolset_tools(
    *,
    storage_provider: "StorageProvider",
    toolset_id: str = SYSTEM_TOOLSET_ID,
) -> dict[str, tuple[Tool, ToolHandler]]:
    """Build the three python-toolset management tools under ``toolset_id``."""

    async def _create_python_toolset_handler(args_json: dict):
        from primer.model.providers.toolset import (
            PythonConfig,
            Toolset,
            ToolsetProviderType,
        )
        from primer.toolset.python_runner.registration import RegistrationError

        args = _CreatePythonToolsetArgs(**args_json)
        try:
            tools = _derived(
                args.source, args.toolset_id, args.default_timeout_seconds
            )
        except RegistrationError as exc:
            return _ok(
                {"ok": False, "error": str(exc), "field": exc.field,
                 "lineno": exc.lineno}
            )
        row = Toolset(
            id=args.toolset_id,
            provider=ToolsetProviderType.PYTHON,
            config=PythonConfig(
                source=args.source,
                source_version=1,
                default_timeout_seconds=args.default_timeout_seconds,
            ),
        )
        await storage_provider.get_storage(Toolset).create(row)
        return _ok({"ok": True, "toolset_id": args.toolset_id, "tools": tools})

    async def _update_python_toolset_source_handler(args_json: dict):
        from primer.model.providers.toolset import Toolset
        from primer.toolset.python_runner.registration import RegistrationError

        args = _UpdatePythonToolsetSourceArgs(**args_json)
        store = storage_provider.get_storage(Toolset)
        existing = await store.get(args.toolset_id)
        if existing is None:
            return _ok({"ok": False, "error": "not-found"})
        try:
            tools = _derived(
                args.source,
                args.toolset_id,
                existing.config.default_timeout_seconds,
            )
        except RegistrationError as exc:
            return _ok(
                {"ok": False, "error": str(exc), "field": exc.field,
                 "lineno": exc.lineno}
            )
        existing.config.source = args.source
        existing.config.source_version += 1
        await store.update(existing)
        return _ok(
            {
                "ok": True,
                "toolset_id": args.toolset_id,
                "source_version": existing.config.source_version,
                "tools": tools,
            }
        )

    async def _list_python_tools_handler(args_json: dict):
        from primer.model.providers.toolset import Toolset
        from primer.toolset.python_runner.registration import RegistrationError

        args = _ListPythonToolsArgs(**args_json)
        existing = await storage_provider.get_storage(Toolset).get(
            args.toolset_id
        )
        if existing is None:
            return _ok({"ok": False, "error": "not-found"})
        try:
            tools = _derived(
                existing.config.source,
                args.toolset_id,
                existing.config.default_timeout_seconds,
            )
        except RegistrationError as exc:
            return _ok({"ok": False, "error": str(exc)})
        return _ok({"ok": True, "toolset_id": args.toolset_id, "tools": tools})

    return {
        "create_python_toolset": (
            make_tool(
                id="create_python_toolset",
                toolset_id=toolset_id,
                purpose=(
                    "Register a python module as a toolset so its functions "
                    "become callable tools."
                ),
                when=(
                    "Use when you need a capability that is easier to express "
                    "as a python function than to assemble from existing "
                    "tools. Every @primer_tool function needs a docstring "
                    "with a 'Use when' line and a documented entry per "
                    "argument, or registration fails and names the offending "
                    "parameter."
                ),
                args_schema=_CreatePythonToolsetArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={
                            "toolset_id": "my-tools",
                            "source": (
                                "@primer_tool()\n"
                                "def greet(name: str) -> str:\n"
                                '    """Greet a person.\n\n'
                                "    Use when greeting someone.\n\n"
                                '    Args:\n        name: Who to greet.\n    """\n'
                                "    return 'hi ' + name\n"
                            ),
                        },
                        returns=(
                            "``{ok: true, toolset_id, tools: "
                            "[{id, yields, args}]}``"
                        ),
                    )
                ],
                required_role="admin",
            ),
            _create_python_toolset_handler,
        ),
        "update_python_toolset_source": (
            make_tool(
                id="update_python_toolset_source",
                toolset_id=toolset_id,
                purpose="Replace a python toolset's module source.",
                when=(
                    "Use when a python tool needs changing. The version is "
                    "bumped so a session parked in one of its tools resumes "
                    "against the code that parked, not the new code."
                ),
                args_schema=(
                    _UpdatePythonToolsetSourceArgs.model_json_schema()
                ),
                examples=[
                    ToolExample(
                        args={
                            "toolset_id": "my-tools",
                            "source": "# new source\n",
                        },
                        returns="``{ok: true, toolset_id, source_version, tools}``",
                    )
                ],
                required_role="admin",
            ),
            _update_python_toolset_source_handler,
        ),
        "list_python_tools": (
            make_tool(
                id="list_python_tools",
                toolset_id=toolset_id,
                purpose=(
                    "List the tools a python toolset's source currently "
                    "derives."
                ),
                when=(
                    "Use when you want to see what a python toolset exposes, "
                    "including whether each tool yields and what arguments it "
                    "takes, without calling any of them."
                ),
                args_schema=_ListPythonToolsArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"toolset_id": "my-tools"},
                        returns=(
                            "``{ok: true, toolset_id, tools: "
                            "[{id, yields, args}]}``"
                        ),
                    )
                ],
                required_role="user",
            ),
            _list_python_tools_handler,
        ),
    }


__all__ = ["build_python_toolset_tools"]
