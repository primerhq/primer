"""ToolsetProvider over a python module.

Registration is done once at construction: the AST pass is pure, and the source
only changes when the toolset record does, at which point the provider is
rebuilt.

Yielding tools register a single module-level resume hook per scoped tool name.
The hook is a module global on purpose - registration is idempotent on the
(name, hook) pair, so rebuilding a provider re-registers the same object rather
than tripping the registry's overwrite guard.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool, ToolCallResult
from primer.model.providers.toolset import PythonConfig
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.python_runner.protocol import (
    PHASE_CALL,
    PHASE_RESUME,
    build_request,
)
from primer.toolset.python_runner.registration import (
    RegisteredTool,
    RegistrationError,
    register_module,
)
from primer.toolset.python_runner.runners import Runner
from primer.toolset.python_runner.yielding import YieldKindError, to_yielded
from primer.worker.yield_resume_registry import ResumeContext, register_resume_hook

logger = logging.getLogger(__name__)

_DEFAULT_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024


def scoped_tool_name(toolset_id: str, tool_id: str) -> str:
    """The name a python tool parks under.

    Scoped because the resume registry is keyed by tool name process-wide and
    python tool names are operator-chosen: two toolsets both defining ``ask``
    would otherwise collide and the second registration would raise.
    """
    return f"{toolset_id}__{tool_id}"


async def python_tool_resume(
    yield_metadata: dict[str, Any],
    event_payload: Any,
    ctx: ResumeContext,
) -> ToolCallResult:
    """Resume hook shared by every python tool.

    One module-level function rather than a closure per tool, so re-registering
    after a provider rebuild is idempotent. Everything it needs to find the
    right toolset comes from the metadata the park stamped.
    """
    toolset_id = (yield_metadata or {}).get("toolset_id")
    tool_id = (yield_metadata or {}).get("tool_id")
    if not toolset_id or not tool_id:
        return ToolCallResult(
            output="this park is missing its toolset attribution and cannot "
            "be resumed",
            is_error=True,
        )
    if ctx.resolve_provider is None:
        return ToolCallResult(
            output=(
                f"{tool_id!r} cannot be resumed from here: this resume path "
                f"has no toolset registry in scope"
            ),
            is_error=True,
        )
    try:
        provider = await ctx.resolve_provider(toolset_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return ToolCallResult(
            output=f"could not reach toolset {toolset_id!r}: {exc}", is_error=True
        )
    if not isinstance(provider, PythonToolsetProvider):
        return ToolCallResult(
            output=f"toolset {toolset_id!r} is no longer a python toolset",
            is_error=True,
        )
    return await provider.resume_tool(
        tool_id=tool_id,
        payload=event_payload,
        resume_metadata=yield_metadata,
    )


class PythonToolsetProvider(ToolsetProvider):
    def __init__(
        self, *, toolset_id: str, config: PythonConfig, runner: Runner
    ) -> None:
        self._toolset_id = toolset_id
        self._config = config
        self._runner = runner
        self._registered: dict[str, RegisteredTool] = {}
        self.registration_error: RegistrationError | None = None

        try:
            for reg in register_module(
                config.source, toolset_id, config.default_timeout_seconds
            ):
                self._registered[reg.tool.id] = reg
        except RegistrationError as exc:
            # A toolset whose source no longer registers lists NOTHING rather
            # than exposing a partial, misleading tool set. The error is kept
            # so the API can show it instead of an empty toolset.
            self.registration_error = exc
            logger.warning(
                "python toolset %s failed to register: %s", toolset_id, exc
            )
            return

        for reg in self._registered.values():
            if reg.resume_fn_name is not None:
                register_resume_hook(
                    scoped_tool_name(toolset_id, reg.tool.id), python_tool_resume
                )

    @property
    def isolation_level(self) -> str:
        return self._runner.isolation_level.value

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for reg in self._registered.values():
            yield reg.tool

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        reg = self._registered.get(tool_name)
        if reg is None:
            return ToolCallResult(
                output=f"no tool named {tool_name!r} in toolset {self._toolset_id!r}",
                is_error=True,
            )
        return await self._invoke(reg, phase=PHASE_CALL, arguments=arguments, ctx=ctx)

    async def resume_tool(
        self,
        *,
        tool_id: str,
        payload: Any,
        resume_metadata: dict[str, Any],
    ) -> ToolCallResult:
        """Second half of a yielding tool. Runs the version that parked."""
        reg = self._registered.get(tool_id)
        if reg is None or reg.resume_fn_name is None:
            return ToolCallResult(
                output=f"{tool_id!r} has no resume hook in toolset "
                f"{self._toolset_id!r}",
                is_error=True,
            )
        parked_version = (resume_metadata or {}).get("source_version")
        if parked_version != self._config.source_version:
            # Running the current source against an answer to the old
            # question is worse than refusing: the operator answered a
            # prompt this code may no longer ask.
            return ToolCallResult(
                output=(
                    f"{tool_id!r} was edited while this call was parked "
                    f"(parked on v{parked_version}, now "
                    f"v{self._config.source_version}); the answer was not "
                    f"applied"
                ),
                is_error=True,
            )
        return await self._invoke(
            reg,
            phase=PHASE_RESUME,
            arguments=None,
            ctx=None,
            payload=_jsonable(payload),
            meta=(resume_metadata or {}).get("tool_meta"),
            fn_name=reg.resume_fn_name,
        )

    async def _invoke(
        self,
        reg: RegisteredTool,
        *,
        phase: str,
        arguments: dict[str, Any] | None,
        ctx: ToolContext | None,
        payload: Any = None,
        meta: Any = None,
        fn_name: str | None = None,
    ) -> ToolCallResult:
        request = build_request(
            module=self._config.source,
            fn=fn_name or reg.fn_name,
            phase=phase,
            args=arguments,
            ctx=_ctx_to_json(ctx),
            payload=payload,
            meta=meta,
            cpu_seconds=int(reg.timeout_seconds) + 1,
            address_space_bytes=_DEFAULT_ADDRESS_SPACE_BYTES,
        )
        request["allow_network"] = self._config.allow_network
        response = await self._runner.run(
            request, timeout_seconds=reg.timeout_seconds
        )

        if response.yield_request is not None:
            if ctx is None:
                return ToolCallResult(
                    output=(
                        f"{reg.tool.id!r} tried to yield outside a session; "
                        f"yielding tools need a session context"
                    ),
                    is_error=True,
                )
            try:
                yielded = to_yielded(
                    response.yield_request,
                    tool_name=scoped_tool_name(self._toolset_id, reg.tool.id),
                    ctx=ctx,
                    source_version=self._config.source_version,
                )
            except YieldKindError as exc:
                return ToolCallResult(output=str(exc), is_error=True)
            # The resume hook is module-level and gets no provider, so the
            # attribution has to travel in the metadata.
            yielded.resume_metadata["toolset_id"] = self._toolset_id
            yielded.resume_metadata["tool_id"] = reg.tool.id
            raise YieldToWorker(yielded, tool_call_id=ctx.tool_call_id)

        if not response.ok:
            err = response.error or {}
            return ToolCallResult(
                output=f"{err.get('type', 'Error')}: {err.get('message', '')}",
                is_error=True,
                extended={"traceback": err.get("traceback", "")},
            )

        value = response.value
        return ToolCallResult(
            output=value if isinstance(value, str) else json.dumps(value),
            is_error=False,
        )


def _jsonable(payload: Any) -> Any:
    """Reduce a resume payload to something the shim can receive.

    YieldTimeout / YieldCancelled are dataclasses, not dicts; the tool's
    resume function sees a plain object either way.
    """
    if payload is None or isinstance(payload, (dict, list, str, int, float, bool)):
        return payload
    out: dict[str, Any] = {"kind": type(payload).__name__}
    for attr in ("reason", "elapsed_seconds", "cancelled_at"):
        if hasattr(payload, attr):
            value = getattr(payload, attr)
            out[attr] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


def _ctx_to_json(ctx: ToolContext | None) -> dict[str, Any]:
    """ToolContext crosses as data only.

    ``inform`` is a callable and ``graph_services`` a live bundle; neither can
    cross a process boundary, so python tools do not receive them.
    """
    if ctx is None:
        return {}
    return {
        "tool_call_id": ctx.tool_call_id,
        "session_id": ctx.session_id,
        "workspace_id": ctx.workspace_id,
        "chat_id": ctx.chat_id,
        "parked_at": ctx.parked_at.isoformat() if ctx.parked_at else None,
    }
