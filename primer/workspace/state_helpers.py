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
        f"{TRAILER_WORKSPACE}: {workspace_id}",
        f"{TRAILER_SESSION}: {session_id}",
        f"{TRAILER_AGENT}: {agent_id}",
        f"{TRAILER_OP}: {op}",
    ]
    if tool is not None:
        trailers.append(f"{TRAILER_TOOL}: {tool}")
    if call_id is not None:
        trailers.append(f"{TRAILER_CALL}: {call_id}")
    return f"{subject}\n\n" + "\n".join(trailers) + "\n"


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
    "validate_session_id",
    "validate_relative_path",
]
