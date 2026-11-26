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
from collections.abc import Awaitable, AsyncIterator, Callable
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

# Cache marker for "this workspace has no sandbox" (a local-backend
# workspace): the answer is stable for the workspace's life, so the
# provider stops re-asking the resolver for it.
_NO_SANDBOX = object()

# The per-workspace sandbox-runner cache is a small LRU: a long-lived process
# must not keep one dead handle per distinct workspace id (X3 round 1,
# M1), so the least-recently-used entry is evicted once the cache is full.
_SANDBOX_RUNNER_CACHE_MAX = 64


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
    """One python toolset: its registered tools and the runner each call gets.

    Runner selection is per CALL, keyed on the calling workspace: a call
    from a container/k8s workspace runs in that workspace's sandbox
    (``SandboxRunner``, isolation ``container``) when ``sandbox_runner_for``
    was supplied at construction; every other call (no workspace, or a
    workspace the resolver says has no sandbox) runs on ``runner`` (the
    hardened local subprocess). A workspace that HAS a sandbox never falls
    back to the local runner: if the sandbox cannot be resolved the call
    fails, because running the tool on the host instead of its container
    would be a downgrade of the isolation the deployment configured.
    """

    def __init__(
        self,
        *,
        toolset_id: str,
        config: PythonConfig,
        runner: Runner,
        sandbox_runner_for: Callable[[str], Awaitable[Runner | None]] | None = None,
    ) -> None:
        self._toolset_id = toolset_id
        self._config = config
        self._runner = runner
        # workspace_id -> the runner resolved for it (or the _NO_SANDBOX
        # marker for a local workspace); a small LRU (_SANDBOX_RUNNER_CACHE_MAX)
        # whose GONE runners are closed as they leave the cache (evicted or
        # replaced by a re-resolution), so a long-lived process does not
        # keep one dead handle per distinct workspace id. A resolution that
        # RAISED is not cached, so the next call retries it.
        self._sandbox_runner_for = sandbox_runner_for
        self._sandbox_runners: dict[str, Runner | object] = {}
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

    @property
    def container_isolation(self) -> bool:
        """Whether a call from a container/k8s workspace runs in its sandbox (the ``container`` level). False where no sandbox resolver was wired (standalone construction, tests)."""
        return self._sandbox_runner_for is not None

    async def _runner_for(self, ctx: ToolContext | None, workspace_id: str | None = None) -> Runner:
        """The runner for one call: the calling workspace's sandbox runner when one resolves, else the local runner."""
        workspace_id = (ctx.workspace_id if ctx is not None else None) or workspace_id
        if not workspace_id or self._sandbox_runner_for is None:
            return self._runner
        cached = self._sandbox_runners.get(workspace_id)
        if cached is _NO_SANDBOX:
            self._touch_lru(workspace_id)
            return self._runner
        if cached is not None and not getattr(cached, "gone", False):
            self._touch_lru(workspace_id)
            return cached
        runner = await self._sandbox_runner_for(workspace_id)
        await self._store_runner(workspace_id, runner or _NO_SANDBOX)
        return runner or self._runner

    def _touch_lru(self, workspace_id: str) -> None:
        """Mark ``workspace_id`` most-recently-used, so an actively used workspace's handle is the last one the LRU drops."""
        if workspace_id in self._sandbox_runners:
            self._sandbox_runners[workspace_id] = self._sandbox_runners.pop(workspace_id)

    async def _store_runner(self, workspace_id: str, runner: Runner | object) -> None:
        """Store the resolved runner for ``workspace_id`` and keep the cache at its bound: the new entry lands at the MRU tail, the least-recently-used entry is dropped at the bound, and a GONE (dead) runner is closed as it leaves the cache.

        Re-storing over a GONE entry (the re-resolve path) closes the
        replaced handle like an eviction would - its client self-evicted
        (a 404 handshake), so the close only releases the aiohttp session
        the dead handle would otherwise keep for the process's life. Only a
        gone handle is ever closed from here: a closed runtime client cannot
        reconnect, and the backend still owns a live workspace
        (X3 round 1, M1; round 1b)."""
        previous = self._sandbox_runners.pop(workspace_id, None)
        self._sandbox_runners[workspace_id] = runner
        if (
            previous is not None
            and previous is not _NO_SANDBOX
            and previous is not runner
            and getattr(previous, "gone", False)
        ):
            await self._close_gone_runner(previous, workspace_id)
        while len(self._sandbox_runners) > _SANDBOX_RUNNER_CACHE_MAX:
            evicted_id, evicted = next(iter(self._sandbox_runners.items()))
            del self._sandbox_runners[evicted_id]
            if evicted is _NO_SANDBOX or not getattr(evicted, "gone", False):
                continue
            await self._close_gone_runner(evicted, evicted_id)

    @staticmethod
    async def _close_gone_runner(runner: Runner, workspace_id: str) -> None:
        """Best-effort close of a GONE runner leaving the cache (replaced or
        evicted): the entry is gone either way, so a failing close is logged,
        never raised."""
        close = getattr(runner, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except Exception:  # noqa: BLE001 - the entry is out of the cache either way
            logger.warning(
                "python toolset: closing the gone sandbox runner of workspace %s failed",
                workspace_id, exc_info=True,
            )

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
        """Second half of a yielding tool, run only on the source version that parked.

        A park made on another version (the source was edited since) is refused: the call ends with an error naming both versions and the
        answer is not applied. The old code is not kept, so it cannot run either.
        """
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
            # The resume rides the runner the call parked on: the park's
            # metadata carries the workspace (to_yielded stamps it), so the
            # resume runs in the same sandbox the call ran in. A park from
            # before the stamp (or one with no workspace) resolves to the
            # local runner, as before.
            workspace_id=(resume_metadata or {}).get("workspace_id"),
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
        workspace_id: str | None = None,
    ) -> ToolCallResult:
        try:
            runner = await self._runner_for(ctx, workspace_id)
        except Exception as exc:
            # The workspace has a sandbox but it could not be resolved.
            # Fail the call rather than downgrade it to the local runner:
            # running the tool on the host instead of its container is a
            # loss of the isolation the deployment configured (see the
            # class docstring).
            failed_ws = (ctx.workspace_id if ctx is not None else None) or workspace_id
            return ToolCallResult(
                output=(
                    f"could not resolve the runner for workspace "
                    f"{failed_ws!r}: {exc}"
                ),
                is_error=True,
            )
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
        response = await runner.run(
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
