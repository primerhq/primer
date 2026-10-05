"""Final-result session relay: post the last-turn outcome to the reply binding.

Symmetric with the outbound gate helpers on each platform adapter,
but for workspace sessions. When a channel event spawns a session the inbound
router stamps a per-session reply binding into ``session.metadata`` (see
:data:`primer.channel.reply_binding.SESSION_REPLY_BINDING_KEY`); otherwise the
session resolves the workspace-standing :attr:`Workspace.reply_binding`. This
helper lets the worker turn loop post one lifecycle signal to that binding:

* :func:`post_session_final_result` -- the last-turn assistant text when the
  session reaches a clean terminal completion.

There is deliberately NO start acknowledgement: the first eager post to a
binding is what GET-OR-CREATES the per-session Discord/Slack thread, so an
unconditional "started" ack opened an empty thread for every session running in
a binding-bearing workspace. Threads now form LAZILY -- only on the first real
gate forward / ``inform`` (``post_prompt``) or a non-empty final result.

:func:`post_session_final_result` no-ops (returns ``False``) when the derived
text is empty, when the session has no reply binding (a non-channel session
stays silent), or when the resolved binding is marked ``quiet`` (per-binding
suppression, spec 8).
"""

from __future__ import annotations

import json
import logging

from primer.channel.adapter import PromptEnvelope
from primer.channel.reply_binding import resolve_reply_binding


_log = logging.getLogger(__name__)

# The records that end a turn's window of assistant text (see derive_session_final_text).
_WINDOW_BOUNDARY_KINDS = ("done", "cancelled", "error")


def _count_reached(results: list) -> int:
    """Count dispatcher results that reached a channel (non-error dicts)."""
    return sum(
        1 for r in results
        if isinstance(r, dict) and "error" not in r
    )


async def _post_lifecycle(
    *,
    dispatcher,
    session,
    storage_provider,
    text: str,
) -> bool:
    """Resolve the binding, honour ``quiet``, post an ``inform`` envelope.

    Returns whether any channel was reached. No-ops (returns ``False``) when
    no binding resolves or the binding is quiet, so a non-channel session is
    silent. Never raises: a dispatch failure is logged and reported as
    ``False`` rather than propagated into the turn loop.
    """
    binding = await resolve_reply_binding(
        session, storage_provider=storage_provider,
    )
    if binding is None or getattr(binding, "quiet", False):
        return False

    env = PromptEnvelope(
        kind="inform",
        workspace_id=session.workspace_id,
        session_id=session.id,
        tool_call_id="",
        prompt=text,
        response_schema=None,
        choices=None,
        timeout_at_iso=None,
        thread_anchor=getattr(binding, "anchor", None),
    )
    try:
        results = await dispatcher.dispatch_prompt(envelope=env, session=session)
    except Exception as exc:  # never raise into the turn loop
        _log.warning(
            "session relay: dispatch failed for %s: %s", session.id, exc,
        )
        return False
    return _count_reached(results) > 0


async def post_session_final_result(
    *,
    dispatcher,
    session,
    storage_provider,
    text: str,
) -> bool:
    """Post the final-result ``text`` to the session's reply binding.

    No-ops (returns ``False``) when ``text`` is empty, when the session has
    no binding, or when the binding is quiet.
    """
    if not text:
        return False
    return await _post_lifecycle(
        dispatcher=dispatcher,
        session=session,
        storage_provider=storage_provider,
        text=text,
    )


def derive_session_final_text(records: list[dict]) -> str | None:
    """Re-derive the final-result text from session ``messages.jsonl`` records.

    Ports the chat ``derive_final_relay_text`` window scan to the session
    surface: ``records`` is the ordered list of parsed ``messages.jsonl`` rows
    (dicts with ``kind`` + ``payload``). The text relayed is the joined
    ``assistant_token`` text of the LAST completed turn, i.e. the rows between
    the previous terminal record and the final ``done`` row. Returns ``None``
    when there is no completed turn, when the LATEST turn did not complete (the
    last terminal record is a ``cancelled``, an ``error`` or a ``done`` whose
    ``stop_reason`` is ``error``), or when the window carries no assistant text.
    A ``done`` with ``stop_reason`` ``max_tokens`` is a truncated answer, not a
    failure, and is relayed.

    A terminal record is a ``done``, a ``cancelled`` or an ``error``. A turn
    that was stopped or failed has no ``done``, but what it had streamed is
    written as ``assistant_token`` records before its ``cancelled`` / ``error``
    record (``flush_partial_output``), and that text must not be joined onto the
    NEXT turn's reply.

    Session assistant tokens carry their text under ``payload['text']`` (the
    coalesced buffer; see :mod:`primer.session.persistence`).
    """
    boundaries = [i for i, r in enumerate(records) if r.get("kind") in _WINDOW_BOUNDARY_KINDS]
    if not boundaries:
        return None
    # The LATEST turn must have completed. If the last terminal record is a ``cancelled`` or an ``error``,
    # the turn did not finish cleanly and there is no final text, even though an earlier ``done`` (the same
    # turn's, when a Cancel landed after the model's terminal event: ``tokens, done, cancelled``; or an
    # earlier turn's) is in the log. Handing that answer to the webhook hold would present the output of a
    # run the user cancelled, or the previous turn's answer, as this run's result.
    last_done = boundaries[-1]
    if records[last_done].get("kind") != "done":
        return None
    # A turn that fails mid-stream can end in ``Done(stop_reason="error")`` with no ``error`` record before it:
    # that ``done`` is a terminal record but the turn did not complete, and its partial text is not a result.
    if (records[last_done].get("payload") or {}).get("stop_reason") == "error":
        return None
    # Assistant text AFTER the last terminal record is a turn that streamed output and never ended. Its
    # ``cancelled`` record is best-effort (skipped when the workspace does not take the write in time), and
    # without this the previous turn's ``done`` would stand for it and that turn's answer would be handed over
    # as this run's result. A user message after the ``done`` is not such a signal (a steer typed as the
    # answer finished), and neither are tool results. The exception is a graph End node's output: it is an
    # ``assistant_token`` carrying ``end_node_id`` that is written AFTER the last node's ``done`` and is the
    # graph's canonical result, so it is not an unfinished turn; it IS the text (a pass-through template
    # writes no such record, and then the last node's answer below stands in).
    end_outputs: list[str] = []
    for r in records[last_done + 1:]:
        if r.get("kind") != "assistant_token":
            continue
        payload = r.get("payload") or {}
        if not payload.get("end_node_id"):
            return None
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            end_outputs.append(text.strip())
    if end_outputs:
        return "\n\n".join(end_outputs)
    prev_boundary = boundaries[-2] if len(boundaries) > 1 else -1
    chunks: list[str] = []
    for r in records[prev_boundary + 1:last_done]:
        if r.get("kind") == "assistant_token":
            text = (r.get("payload") or {}).get("text")
            if isinstance(text, str):
                chunks.append(text)
    out = "".join(chunks).strip()
    return out or None


async def read_session_final_text(workspace_io, session_id: str) -> str | None:
    """Read ``messages.jsonl`` for ``session_id`` and derive the final text.

    Reads the per-session ``messages.jsonl`` through whichever read surface the
    workspace IO exposes (the concrete backends offer ``read_file`` over the
    state path; test fakes expose ``read_lines``), parses each line to a record
    dict, and runs :func:`derive_session_final_text`. Returns ``None`` and never
    raises on any read/parse error so the relay degrades silently.
    """
    lines: list[str] = []
    read_lines = getattr(workspace_io, "read_lines", None)
    if callable(read_lines):
        try:
            result = read_lines(session_id)
            lines = list(result) if result is not None else []
        except Exception:
            return None
    else:
        read_file = getattr(workspace_io, "read_file", None)
        if not callable(read_file):
            return None
        state_path = getattr(workspace_io, "state_path", ".state")
        path = f"{state_path}/sessions/{session_id}/messages.jsonl"
        try:
            raw = await read_file(path)
        except Exception:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        lines = raw.splitlines()

    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
    return derive_session_final_text(records)


__all__ = [
    "derive_session_final_text",
    "post_session_final_result",
    "read_session_final_text",
]
