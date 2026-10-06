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
from primer.model.except_ import NotFoundError
from primer.session.terminals import is_delegated, is_session_terminal


_log = logging.getLogger(__name__)

# What a channel is told when a turn was stopped by the agent's ``max_tool_turns`` instead of finishing: said as a
# notice, never posted as if it were the answer (the partial text, when there is any, follows it).
TOOL_TURN_CAP_NOTICE = "This run stopped at its tool-turn cap (the agent's max_tool_turns) before it finished."


def stopped_short_message(partial_text: str | None) -> str:
    """The message a tool-turn-cap trip posts: the notice, then what the agent had so far when it had something."""
    partial = (partial_text or "").strip()
    return f"{TOOL_TURN_CAP_NOTICE}\n\nWhat it had so far:\n\n{partial}" if partial else TOOL_TURN_CAP_NOTICE



def _count_reached(results: list) -> int:
    """Count dispatcher results that reached a channel (non-error dicts)."""
    return sum(
        1 for r in results
        if isinstance(r, dict) and "error" not in r
    )


# "Work the binding out": the default for a caller that has not already resolved it. A sentinel and not None, because
# None is a resolved answer ("this session has no binding") that a caller may pass in to say so and skip the lookup.
_RESOLVE = object()


async def _post_lifecycle(
    *,
    dispatcher,
    session,
    storage_provider,
    text: str,
    binding=_RESOLVE,
) -> bool:
    """Resolve the binding, honour ``quiet``, post an ``inform`` envelope.

    Returns whether any channel was reached. No-ops (returns ``False``) when
    no binding resolves or the binding is quiet, so a non-channel session is
    silent. Never raises: a dispatch failure is logged and reported as
    ``False`` rather than propagated into the turn loop.
    """
    if binding is _RESOLVE:
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
    binding=_RESOLVE,
) -> bool:
    """Post the final-result ``text`` to the session's reply binding.

    No-ops (returns ``False``) when ``text`` is empty, when the session has
    no binding, or when the binding is quiet. A caller that has already
    resolved the binding (the dispatch does, to decide whether to read the
    text at all) passes it as ``binding`` and it is not resolved again.
    """
    if not text:
        return False
    return await _post_lifecycle(
        dispatcher=dispatcher,
        session=session,
        storage_provider=storage_provider,
        text=text,
        binding=binding,
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
    # A window ends at a terminal of the SESSION's own run (a ``done`` / ``cancelled`` / ``error``; a model call that ended in
    # a tool call is one, so what is relayed is the text after the last round). A subagent's terminal (payload.delegated)
    # is the end of the subagent's turn: as a boundary it made the subagent's last words the session's result whenever
    # the parent died after it (its own ``cancelled`` / ``error`` write is best-effort).
    boundaries = [i for i, r in enumerate(records) if is_session_terminal(r)]
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
    # Only TOP-LEVEL End outputs are the result. A subgraph's End output forwarded by its parent is marked
    # ``nested`` and is left out (joining it would read "Inner: ...\n\nOuter: ..."). The one exception: when no
    # top-level End output was written at all, the last nested one stands in, because the parent's End that
    # echoed it was suppressed as a duplicate of it (see persistence's _GraphEndOutputEvent branch), so it
    # carries exactly that End's text.
    end_outputs: list[str] = []
    nested_outputs: list[str] = []
    for r in records[last_done + 1:]:
        if r.get("kind") != "assistant_token":
            continue
        payload = r.get("payload") or {}
        if not payload.get("end_node_id"):
            return None
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            (nested_outputs if payload.get("nested") else end_outputs).append(text.strip())
    if end_outputs:
        return "\n\n".join(end_outputs)
    if nested_outputs:
        return nested_outputs[-1]
    prev_boundary = boundaries[-2] if len(boundaries) > 1 else -1
    chunks: list[str] = []
    for r in records[prev_boundary + 1:last_done]:
        # The subagents' text sits inside the parent's window now that their terminals are not boundaries; it is theirs.
        if r.get("kind") == "assistant_token" and not is_delegated(r):
            text = (r.get("payload") or {}).get("text")
            if isinstance(text, str):
                chunks.append(text)
    out = "".join(chunks).strip()
    return out or None


def _lines_from_the_end(raw: "bytes | str"):
    """The ``\\n``-terminated lines of ``raw``, last first, without splitting the whole text into a list.

    ``messages.jsonl`` is one JSON record per ``\\n``-terminated line (the writers append ``model_dump_json() + "\\n"``); splitting
    on that byte alone, not on ``str.splitlines()``, keeps a record whole when its text holds a character ``splitlines`` also
    breaks at (U+2028, U+0085, a form feed): ``model_dump_json`` writes those unescaped, and cutting the line there made the
    record unparseable, so it was silently dropped.
    """
    newline = b"\n" if isinstance(raw, bytes) else "\n"
    end = len(raw)
    while end > 0:
        start = raw.rfind(newline, 0, end) + 1  # 0 when there is no earlier newline
        yield raw[start:end]
        end = start - 1


def _parse_tail(lines_last_first) -> list[dict]:
    """Parse ``messages.jsonl`` lines, last first, only as far back as the final-text window reaches; return them in file order.

    ``derive_session_final_text`` looks at the records from the second-to-last terminal record on (the window of the last
    completed turn is the rows between the previous terminal record and the final ``done``, and what follows the final one), and
    ignores everything before. Parsing stops once two terminal records have been seen, with the second one INCLUDED, so the
    result has the same last two boundaries as the whole file would and the same answer; if the file has fewer than two it is
    parsed whole. The cost is the size of the last window, not of the session's history (a relay-every-turn session read and parsed
    all of it after every turn). Lines that are blank, do not parse, or parse to something that is not a record are skipped
    (the last kind used to reach ``derive_session_final_text`` and raise ``AttributeError`` there).
    """
    tail: list[dict] = []
    boundaries = 0
    for line in lines_last_first:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict):
            continue  # a line that is valid JSON but not a record (a stray number or list): skipped like an unparseable one
        tail.append(record)
        if is_session_terminal(record):
            boundaries += 1
            if boundaries == 2:
                break
    tail.reverse()
    return tail


async def read_session_final_text(workspace_io, session_id: str) -> str | None:
    """Read ``messages.jsonl`` for ``session_id`` and derive the final text.

    Reads the per-session ``messages.jsonl`` through whichever read surface the
    workspace IO exposes (the concrete backends offer ``read_file`` over the
    state path; test fakes expose ``read_lines``), parses the records of the
    last window (see :func:`_parse_tail`: from the second-to-last terminal
    record on, not the whole history), and runs
    :func:`derive_session_final_text`. Returns ``None`` and never raises on any
    read/parse error so the relay degrades silently.

    The file is still read whole (the workspace read surface has no ranged
    read), so on a docker or k8s workspace the bytes still cross the runtime
    connection; what no longer grows with the session is the decode and the
    JSON parse.
    """
    lines_last_first = None
    read_lines = getattr(workspace_io, "read_lines", None)
    if callable(read_lines):
        try:
            result = read_lines(session_id)
            lines_last_first = reversed(list(result) if result is not None else [])
        except Exception:
            _log.warning("session relay: read_lines failed for %s", session_id, exc_info=True)
            return None
    else:
        read_file = getattr(workspace_io, "read_file", None)
        if not callable(read_file):
            # The relay's silent failure for months: handed a write-only adapter (the pool's ``_WorkspaceIOShim``),
            # there is nothing to read through and the answer is never found. Hand it the workspace.
            _log.warning(
                "session relay: %s has neither read_lines nor read_file; the final text of %s cannot be read",
                type(workspace_io).__name__, session_id,
            )
            return None
        state_path = getattr(workspace_io, "state_path", ".state")
        path = f"{state_path}/sessions/{session_id}/messages.jsonl"
        try:
            raw = await read_file(path)
        except NotFoundError:
            return None  # no messages.jsonl yet: nothing to relay, and not a fault
        except Exception:
            _log.warning("session relay: reading %s failed", path, exc_info=True)
            return None
        lines_last_first = _lines_from_the_end(raw)

    return derive_session_final_text(_parse_tail(lines_last_first))


__all__ = [
    "derive_session_final_text",
    "post_session_final_result",
    "read_session_final_text",
]
