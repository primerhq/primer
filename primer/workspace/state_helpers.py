"""Shared state-repo helpers for the local + sandbox StateRepo impls.

:class:`primer.workspace.local.state.LocalStateRepo` (host git) and
:class:`primer.workspace.sandbox.state.SandboxStateRepo` (in-container
runtime ops) must produce byte-compatible commit messages and apply the
same path / session-id validation so the conformance suite
(``tests/workspace/test_state_repo_conformance.py``) can treat them
interchangeably. This module is the single source of truth for the
trailer keys, the valid ``op`` set, the commit-message builders, and the
input validators both implementations share.

NOTE: the formal ``@runtime_checkable`` ``StateRepo`` Protocol lives at
:mod:`primer.int.state_repo`; this module deliberately does NOT recreate
it -- it only hosts the concrete helpers.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath


# ---------------------------------------------------------------------------
# Trailer keys -- machine-readable identifiers in the commit body.
# ---------------------------------------------------------------------------

TRAILER_WORKSPACE = "X-Primer-Workspace"
TRAILER_SESSION = "X-Primer-Session"
TRAILER_AGENT = "X-Primer-Agent"
TRAILER_OP = "X-Primer-Op"
TRAILER_TOOL = "X-Primer-Tool"
TRAILER_CALL = "X-Primer-Call"


# Allowed values of the ``op`` trailer (canonical type lives in
# ``primer.model.workspace.Op``; this set is the runtime validator).
VALID_OPS: frozenset[str] = frozenset(
    [
        "attach",
        "message",
        "user_instruction",
        "tool_call",
        "tool_result",
        "memory_write",
        "todo_update",
        "status_change",
        "rename",
    ]
)


# ---------------------------------------------------------------------------
# Commit-message builder
# ---------------------------------------------------------------------------


# Every character the history parsers or the process act on: all C0 controls (CR and LF end a line; VT, FF, FS, GS end one for ``str.splitlines``; RS and
# US are the RECORD and FIELD separators of ``git log --format`` in both parsers; NUL cannot be passed to git in an argument; tab, ESC and the rest a
# terminal or a log viewer acts on), DEL, and NEL, U+2028 and U+2029, which ``str.splitlines`` treats as line ends.
_UNSAFE_COMMIT_CHARS = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


def commit_text(value: object) -> str:
    """``value`` as text that is safe to put in a commit message: every control or line-separating character becomes a space (security ticket 01a11fbc-ffea).

    A commit message is read back by two parsers (``LocalStateRepo.history()`` and the runtime's ``_parse_log_records``) that split on lines, on RS between
    records and on US between fields. Text a caller controls (a workspace id, a failure code, a provider's tool_call_id, a session's name in the subject of
    its rename commit, a graph node id) that held any of them forged trailers, numstat files or a whole record, made ``history()`` raise for every window
    containing the commit, or failed the commit outright (NUL). Text without one is returned exactly as it was.
    """
    return _UNSAFE_COMMIT_CHARS.sub(" ", str(value))


def has_unsafe_commit_chars(value: str) -> bool:
    """Whether ``value`` holds a character :func:`commit_text` would replace (the API refuses such a name rather than writing a changed one)."""
    return _UNSAFE_COMMIT_CHARS.search(value) is not None


def trailer_line(key: str, value: object) -> str:
    """``key: value`` as ONE line of a commit message: both pass through :func:`commit_text`.

    The trailer writers are ``build_message`` (session-scoped commits) and ``commit_arbitrary`` of both backends. ``X-Primer-Call`` and ``X-Primer-Tool``
    have no production writer today (only ``build_message``'s optional arguments, which no caller fills); they are covered as defence in depth.
    """
    return f"{commit_text(key)}: {commit_text(value)}"


def build_message(
    *,
    subject: str,
    workspace_id: str,
    session_id: str,
    agent_id: str,
    op: str,
    tool: str | None,
    call_id: str | None,
) -> str:
    """Build a commit message with trailers in the order the spec dictates.

    Both StateRepo implementations call this so their commit bodies are
    byte-compatible.
    """
    trailers = [
        trailer_line(TRAILER_WORKSPACE, workspace_id),
        trailer_line(TRAILER_SESSION, session_id),
        trailer_line(TRAILER_AGENT, agent_id),
        trailer_line(TRAILER_OP, op),
    ]
    if tool is not None:
        trailers.append(trailer_line(TRAILER_TOOL, tool))
    if call_id is not None:
        trailers.append(trailer_line(TRAILER_CALL, call_id))
    return f"{commit_text(subject)}\n\n" + "\n".join(trailers) + "\n"


# ---------------------------------------------------------------------------
# Input validators
# ---------------------------------------------------------------------------


def validate_session_id(session_id: str) -> None:
    """Reject session ids that would let writes escape the slot."""
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(
            f"session_id contains illegal characters: {session_id!r}"
        )
    if "\x00" in session_id:
        raise ValueError("session_id contains a null byte")


def validate_relative_path(rel: str) -> None:
    """Reject paths that would escape the session slot."""
    if not rel:
        raise ValueError("path must be non-empty")
    if rel.startswith("/") or rel.startswith("\\"):
        raise ValueError(f"path must be relative: {rel!r}")
    parts = PurePosixPath(rel).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"path must not contain '..': {rel!r}")
    if "\x00" in rel:
        raise ValueError("path contains a null byte")


__all__ = [
    "TRAILER_WORKSPACE",
    "TRAILER_SESSION",
    "TRAILER_AGENT",
    "TRAILER_OP",
    "TRAILER_TOOL",
    "TRAILER_CALL",
    "VALID_OPS",
    "build_message",
    "commit_text",
    "has_unsafe_commit_chars",
    "trailer_line",
    "validate_session_id",
    "validate_relative_path",
]
