"""The tool alias rule for agent-runtime bridges.

A scoped tool id is listed on the bridge under an alias short enough for the
runtime's tool namespace. ``claude_code`` and ``codex`` differ in their length
limits and in whether the alias must stay within the runtime's own character
set, so the rule takes its limits as parameters.
"""

from __future__ import annotations

import hashlib
import re

#: The characters an alias may contain when ``sanitize=True``.
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def tool_alias(scoped_id: str, *, max_len: int, keep_len: int, sanitize: bool = False) -> str:
    """The alias a scoped tool id is listed under.

    Without sanitizing, an id of at most ``max_len`` characters passes through
    unchanged; a longer one becomes its first ``keep_len`` characters, an
    underscore, and the first 8 hex characters of the SHA-256 of the FULL id,
    so two ids sharing a long prefix still get distinct aliases that map back
    to their own id.

    With sanitizing, an id passes through unchanged only when it is at most
    ``max_len`` characters AND matches ``[A-Za-z0-9_-]+``. Otherwise it becomes
    the first ``keep_len`` characters with every character outside
    ``[A-Za-z0-9_-]`` replaced by ``_``, then an underscore and the same 8 hex
    characters of the ORIGINAL id's SHA-256.
    """
    if len(scoped_id) <= max_len and (not sanitize or re.fullmatch(r"[A-Za-z0-9_-]+", scoped_id)):
        return scoped_id
    if sanitize:
        head = _SAFE.sub("_", scoped_id[:keep_len])
    else:
        head = scoped_id[:keep_len]
    return head + "_" + hashlib.sha256(scoped_id.encode("utf-8")).hexdigest()[:8]
