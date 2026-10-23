"""Turn-time listing of the toolsets whose tools live behind a network or a process (platform revamp, design 20 Q4).

A turn starts by listing the tools of every toolset its agent uses. A toolset that is only a table in this process
cannot fail to list, so when one does it is a bug and still ends the turn. An MCP toolset can fail for reasons that
are nobody's bug at that moment: the server is down, it needs an operator to sign in, or it accepts the connection
and never answers. That must not end every turn of every agent that uses it. So a provider that sets
``ToolsetProvider.lists_remotely`` is listed here instead, each toolset bounded at :data:`TURN_TOOLSET_LIST_TIMEOUT_S`,
and one that cannot list is LEFT OUT of the turn. The model is told in the system message of the prompt it is sent
(:func:`with_system_notice`; the notice is never persisted), in a fixed vocabulary only (:data:`NOTICE_STATES`: needs
sign-in, unreachable, timed out, failed), because the system message outranks everything else the model reads and the
reason a listing fails can be the server's own text (a JSON-RPC error of ``tools/list``; audit G4-14). The reason itself
(credentials masked, one line, cut) answers a call that names one of the left-out toolset's tools, at a tool result's
rank (:meth:`primer.agent.tool_manager.ToolExecutionManager.unavailable_toolsets`).

Cancellation is the reason for the shape of :func:`list_remote_toolset`. Each listing runs in its OWN task and is awaited
with ``asyncio.wait``, which does not raise when the listing fails and raises ``CancelledError`` only when the CALLING
task is cancelled (a Stop delivered through :func:`primer.agent.interrupt.interruptible`, a session Cancel, a
shutdown). So:

* A cancellation that belongs to the caller is never turned into "this toolset is unavailable". The listing is cancelled
  with it, given a moment to close its connection, and the ``CancelledError`` goes on. ``uncancel()`` is never called, so
  an enclosing ``asyncio.timeout`` (the Stop scope) keeps its own count and still converts the cancel it raised.
* A cancellation that belongs to the listing stays in the listing. The MCP SDK runs anyio task groups, and the stdio
  handshake absorbs the cancels its scope leaks (``McpToolsetProvider._enter_stdio_session``); run in the caller's task
  either would eat or leave behind a cancel that is the caller's (tests/agent/test_subagent_stop.py is the incident).
  In its own task they are contained, and a listing that ends cancelled without the caller being cancelled is simply
  a toolset that could not list.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from primer.common.log import redact_credentials
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Message, TextPart, Tool

logger = logging.getLogger(__name__)

#: How long a turn waits for ONE remote toolset to list its tools. Generous on purpose: a cold ``npx`` start of a stdio
#: MCP server takes tens of seconds. Callers read it as ``tool_listing.TURN_TOOLSET_LIST_TIMEOUT_S`` at the moment they
#: list, not at import, so a test can lower it.
TURN_TOOLSET_LIST_TIMEOUT_S: float = 60.0

#: The first line of the notice the model reads.
NOTICE_HEADER = "Toolsets unavailable this turn, so their tools are not offered:"

#: What the notice says of a toolset left out of the turn: one of these words, never text from the server (audit G4-14).
NEEDS_SIGN_IN = "needs sign-in"
UNREACHABLE = "unreachable"
TIMED_OUT = "timed out"
FAILED = "failed"
NOTICE_STATES = frozenset({NEEDS_SIGN_IN, UNREACHABLE, TIMED_OUT, FAILED})

#: How long a listing that has been cancelled gets to close its connection (a stdio server is given 2 s to exit before it
#: is killed) before the turn stops waiting for it. It finishes on its own after that; nothing waits on it.
_UNWIND_GRACE_S = 5.0

_MAX_REASON_CHARS = 200
_TORN_DOWN = "its connection was torn down while listing"

# The listings that are still running. The event loop holds a task only weakly, so one that outlives its wait (it did
# not unwind within the grace) is kept here until it ends rather than being collected half torn down.
_RUNNING: set[asyncio.Task[list[Tool]]] = set()


@dataclass(frozen=True)
class ToolListing:
    """What one remote toolset answered: its tools, or why it could not list them (``reason`` and ``state`` set, ``tools`` empty).

    ``reason`` is the account a call into the toolset answers with (it can hold the server's text); ``state`` is the word the
    system message says (:data:`NOTICE_STATES`). ``error`` is the exception the listing failed with (``None`` for a listing that
    answered, timed out or was torn down). It is for the DEBUG log only (the traceback of a bug inside the provider), never
    shown to the model, and not compared.
    """

    tools: list[Tool]
    reason: str | None
    error: BaseException | None = field(default=None, compare=False, repr=False)
    state: str | None = None


def _retire(task: asyncio.Task[list[Tool]]) -> None:
    _RUNNING.discard(task)
    if not task.cancelled():
        task.exception()  # read, so a listing nobody awaits any more does not log "exception was never retrieved"


async def _drain(provider: ToolsetProvider, principal: str | None) -> list[Tool]:
    return [tool async for tool in provider.list_tools(principal=principal)]


async def _unwind(task: asyncio.Task[list[Tool]], label: str) -> None:
    """Wait, bounded, for a listing that has been cancelled to finish. A cancel of the caller that arrives meanwhile goes through."""
    if not task.done():
        await asyncio.wait({task}, timeout=_UNWIND_GRACE_S)
    if not task.done():
        logger.warning(
            "a toolset listing did not stop within %gs of being cancelled; it is left to finish on its own",
            _UNWIND_GRACE_S,
            extra={"toolset": label},
        )


def _reason_for(failure: BaseException) -> str:
    """Why a listing failed, as one short line fit for a prompt: the primer error's message, credentials masked, cut."""
    # Imported here: the classifier pulls in the MCP SDK (half a second), which the agent package otherwise never needs.
    from primer.common.mcp_errors import classify_mcp_exception

    cause: BaseException | None = failure
    if isinstance(cause, BaseExceptionGroup):
        # anyio task groups wrap what went wrong. A group of only cancellations is a connection torn down, not a failure
        # with a message; anything else is classified (the classifier unwraps the group to its most informative leaf).
        _cancelled, cause = cause.split(asyncio.CancelledError)
        if cause is None:
            return _TORN_DOWN
    message = classify_mcp_exception(cause).message  # type: ignore[arg-type]
    # Masked BEFORE it is cut: a secret that straddles the cut keeps its head otherwise. One line: this goes into a system
    # message as one bullet, so a server that sends newlines cannot add lines (or a header) of its own.
    text = " ".join(redact_credentials(message).split()) or type(failure).__name__
    return text if len(text) <= _MAX_REASON_CHARS else text[: _MAX_REASON_CHARS - 3] + "..."


_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError,)


def _state_for(failure: BaseException) -> str:
    """The notice's word for a failed listing: needs sign-in, unreachable, timed out or failed (:data:`NOTICE_STATES`)."""
    import httpx
    import httpx2

    from primer.common.mcp_errors import classify_mcp_exception
    from primer.model.except_ import AuthRequiredError, NetworkError
    from primer.model.mcp_oauth import McpSignInRequiredError

    cause: BaseException | None = failure
    if isinstance(cause, BaseExceptionGroup):
        _cancelled, cause = cause.split(asyncio.CancelledError)
        if cause is None:
            return FAILED
    leaves: list[BaseException] = []
    pending: list[BaseException] = [cause]  # type: ignore[list-item]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        else:
            leaves.append(current)
    if any(isinstance(leaf, (*_TIMEOUT_ERRORS, httpx.TimeoutException, httpx2.TimeoutException)) for leaf in leaves):
        return TIMED_OUT
    classified = classify_mcp_exception(cause)  # type: ignore[arg-type]
    if isinstance(classified, (McpSignInRequiredError, AuthRequiredError)):
        return NEEDS_SIGN_IN
    if isinstance(classified, NetworkError):
        return UNREACHABLE
    return FAILED


async def list_remote_toolset(provider: ToolsetProvider, *, principal: str | None, timeout_s: float) -> ToolListing:
    """List ``provider``'s tools, waiting at most ``timeout_s`` seconds; never raises for a listing that fails.

    A failure (an error, a timeout, a connection torn down) comes back as a :class:`ToolListing` with a ``reason``. The
    only exception that comes out is the CALLER's own cancellation: see the module docstring.
    """
    label = str(getattr(provider, "_toolset_id", type(provider).__name__))
    task = asyncio.create_task(_drain(provider, principal), name=f"toolset-listing:{label}")
    _RUNNING.add(task)
    task.add_done_callback(_retire)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError:
        # The wait cancels nothing by itself, and this is the caller's cancellation: stop the listing and let it close its
        # connection, then re-raise. Not absorbed, not uncancel()led.
        task.cancel()
        await _unwind(task, label)
        raise
    if not done:
        task.cancel()
        await _unwind(task, label)
        return ToolListing(tools=[], reason=f"did not list its tools within {timeout_s:g}s", state=TIMED_OUT)
    if task.cancelled():
        # Cancelled, yet the wait above was not: not the caller's. The SDK's own scope ended the listing.
        return ToolListing(tools=[], reason=_TORN_DOWN, state=FAILED)
    failure = task.exception()
    if failure is None:
        return ToolListing(tools=task.result(), reason=None)
    if isinstance(failure, (KeyboardInterrupt, SystemExit)):
        raise failure
    return ToolListing(tools=[], reason=_reason_for(failure), error=failure, state=_state_for(failure))


def unavailable_toolsets_notice(unavailable: Mapping[str, str]) -> str | None:
    """The notice the model reads for the toolsets left out of this turn (toolset id to its state), or ``None`` when none was.

    Each toolset is said with a word of :data:`NOTICE_STATES` only: anything else it is handed (a manager that passes a reason, a
    server's text) is said as ``failed``, so no text from a server reaches the system message (audit G4-14).
    """
    if not isinstance(unavailable, Mapping) or not unavailable:
        return None
    return "\n".join([
        NOTICE_HEADER,
        *(f"- {toolset_id}: {state if state in NOTICE_STATES else FAILED}" for toolset_id, state in unavailable.items()),
    ])


def with_system_notice(messages: list[Message], notice: str | None) -> list[Message]:
    """``messages`` as they are SENT: ``notice`` added to the first system message's text, or in a system message of its own.

    Never changes the messages it is given: the prompt a turn keeps, and the messages it persists, do not carry the
    notice (it describes THIS turn's catalogue, and the next turn lists again).
    """
    if not notice:
        return messages
    for index, message in enumerate(messages):
        if message.role != "system":
            continue
        parts = list(message.parts)
        for at in range(len(parts) - 1, -1, -1):
            part = parts[at]
            if isinstance(part, TextPart):
                parts[at] = part.model_copy(update={"text": f"{part.text}\n\n{notice}"})
                break
        else:
            parts.append(TextPart(text=notice))
        sent = list(messages)
        sent[index] = message.model_copy(update={"parts": parts})
        return sent
    return [Message(role="system", parts=[TextPart(text=notice)]), *messages]


__all__ = [
    "FAILED",
    "NEEDS_SIGN_IN",
    "NOTICE_HEADER",
    "NOTICE_STATES",
    "TIMED_OUT",
    "UNREACHABLE",
    "TURN_TOOLSET_LIST_TIMEOUT_S",
    "ToolListing",
    "list_remote_toolset",
    "unavailable_toolsets_notice",
    "with_system_notice",
]
