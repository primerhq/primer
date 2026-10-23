"""What a non-admin may see of the platform event log: ONE rule, for the REST feed and for ``wait_for_event``.

``GET /v1/events`` (``primer/api/routers/events.py``) gives a non-admin a workspace feed of the operational lifecycle, never entity CRUD
(whose payloads mirror stored rows). ``system__wait_for_event`` is open to user-role runs, and the event that wakes it becomes the run's
tool result, so a run whose principal is not an admin may wait only on those same kinds (``primer/toolset/events_wait.py``). Both read
this module, so the two cannot drift. Prefixes and exact names, checked against the event type: the allowlist bounds the SHAPE of what a
non-admin sees, redaction (:mod:`primer.events.redaction`) bounds the values.

The canvas events (``canvas.updated`` and ``canvas.event``, pivot P5) are visible like the session events: the canvas is part of every
user's chat in the shared install, and what they carry (element ids, the rev, the user's own validated choice or click and who made it)
is what the chat transcript already shows. Their values are bounded where they are recorded (``primer.canvas.service``) and masked by
the same redaction as the rest. They are the first user-safe kinds that carry what a user ENTERED (a value, Html emit data), so a
non-admin ``wait_for_event`` on them must also name the workspace or the session they are for (:func:`needs_scope`; audit G8-08).
"""

from __future__ import annotations

#: Event-type prefixes a non-admin may see: the session and workspace lifecycle, and the session canvas.
USER_SAFE_PREFIXES: tuple[str, ...] = ("session.", "workspace.", "canvas.")
#: Exact event types a non-admin may see.
USER_SAFE_TYPES: frozenset[str] = frozenset({"tool.called", "turn.started", "trigger.fired", "llm.called"})
#: Event-type prefixes a non-admin may wait on only with a workspace or session matcher: the canvas events carry user-entered content.
SCOPED_PREFIXES: tuple[str, ...] = ("canvas.",)
#: The envelope fields a non-admin's ``eq`` matcher names to scope such a wait.
SCOPE_PATHS: tuple[str, ...] = ("workspace_id", "session_id")

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


def needs_scope(glob: str) -> bool:
    """Whether the ``fnmatch`` glob ``glob`` can match a kind of :data:`SCOPED_PREFIXES` (``canvas.event``, ``canvas.*``, ``canvas.e?ent``).

    Judged on the literal head, as :func:`user_safe_glob` is: a head that begins with a scoped prefix, or that the prefix begins with
    (``canv*``, which :func:`user_safe_glob` refuses anyway), can reach one.
    """
    cut = min((glob.find(c) for c in _GLOB_CHARS if c in glob), default=-1)
    head = glob if cut < 0 else glob[:cut]
    return any(head.startswith(p) or p.startswith(head) for p in SCOPED_PREFIXES)


def user_safe_kinds() -> list[str]:
    """The kinds a non-admin may see, as globs (``session.*`` for a prefix), for messages and tool descriptions."""
    return [f"{p}*" for p in USER_SAFE_PREFIXES] + sorted(USER_SAFE_TYPES)


__all__ = [
    "SCOPED_PREFIXES",
    "SCOPE_PATHS",
    "USER_SAFE_PREFIXES",
    "USER_SAFE_TYPES",
    "needs_scope",
    "user_safe",
    "user_safe_glob",
    "user_safe_kinds",
]
