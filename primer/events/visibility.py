"""What a non-admin may see of the platform event log: ONE rule, for the REST feed and for ``wait_for_event``.

``GET /v1/events`` (``primer/api/routers/events.py``) gives a non-admin a workspace feed of the operational lifecycle, never entity CRUD
(whose payloads mirror stored rows). ``system__wait_for_event`` is open to user-role runs, and the event that wakes it becomes the run's
tool result, so a run whose principal is not an admin may wait only on those same kinds (``primer/toolset/events_wait.py``). Both read
this module, so the two cannot drift. Prefixes and exact names, checked against the event type: the allowlist bounds the SHAPE of what a
non-admin sees, redaction (:mod:`primer.events.redaction`) bounds the values.
"""

from __future__ import annotations

#: Event-type prefixes a non-admin may see: the session and workspace lifecycle.
USER_SAFE_PREFIXES: tuple[str, ...] = ("session.", "workspace.")
#: Exact event types a non-admin may see.
USER_SAFE_TYPES: frozenset[str] = frozenset({"tool.called", "turn.started", "trigger.fired", "llm.called"})

# What makes an ``fnmatch`` glob more than a literal name (the dispatcher matches ``event_types`` with ``fnmatchcase``,
# ``primer.events.filters.matches``; the REST route reads a glob's literal head the same way).
_GLOB_CHARS = "*?["


def user_safe(event_type: str) -> bool:
    """Whether a non-admin may see an event of type ``event_type``."""
    return event_type in USER_SAFE_TYPES or any(event_type.startswith(p) for p in USER_SAFE_PREFIXES)


def user_safe_glob(glob: str) -> bool:
    """Whether EVERY event type the ``fnmatch`` glob ``glob`` can match is one a non-admin may see (:func:`user_safe`).

    A glob with no wildcard names one type and is judged as that type. Any other glob can match every type that begins with its literal
    head (the text before its first wildcard), so it passes only when that head already begins with a safe PREFIX (``session.*``,
    ``workspace.file_*``). A glob that could also reach another kind (``*``, ``agent.*``, ``turn.*``, ``session*``) does not pass, even
    when part of what it matches would: name the safe kinds instead.
    """
    cut = min((glob.find(c) for c in _GLOB_CHARS if c in glob), default=-1)
    if cut < 0:
        return user_safe(glob)
    head = glob[:cut]
    return any(head.startswith(p) for p in USER_SAFE_PREFIXES)


def user_safe_kinds() -> list[str]:
    """The kinds a non-admin may see, as globs (``session.*`` for a prefix), for messages and tool descriptions."""
    return [f"{p}*" for p in USER_SAFE_PREFIXES] + sorted(USER_SAFE_TYPES)


__all__ = ["USER_SAFE_PREFIXES", "USER_SAFE_TYPES", "user_safe", "user_safe_glob", "user_safe_kinds"]
