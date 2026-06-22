"""Final-result session relay: post the last-turn outcome to the reply binding.

Symmetric with the chat relay helpers in :mod:`primer.channel.chat_dispatcher`,
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
    the previous and final ``done`` rows. Returns ``None`` when there is no
    completed turn or the window carries no assistant text.

    Session assistant tokens carry their text under ``payload['text']`` (the
    coalesced buffer; see :mod:`primer.session.persistence`).
    """
    last_done = max(
        (i for i, r in enumerate(records) if r.get("kind") == "done"),
        default=None,
    )
    if last_done is None:
        return None
    prev_done = max(
        (i for i in range(last_done) if records[i].get("kind") == "done"),
        default=-1,
    )
    chunks: list[str] = []
    for r in records[prev_done + 1:last_done]:
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
