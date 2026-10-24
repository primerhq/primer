"""The id format of the artifacts a non-database backend mints (filesystem, S3).

A backend id is ``artifact-`` and 32 lowercase hex digits (a uuid4). Database ids stay ``artifact-`` and 12 hex digits (``Identifiable``'s
own format), so the two never collide. The filesystem store turns an id into a path, so an id is checked against the pattern before it is
used; the pattern is anchored with ``\\A`` and ``\\Z`` because ``$`` would also match before a trailing newline.
"""

from __future__ import annotations

import re
from uuid import uuid4

BACKEND_ARTIFACT_ID_RE = re.compile(r"\Aartifact-[0-9a-f]{32}\Z")


def new_backend_artifact_id() -> str:
    """A fresh backend id: ``artifact-`` and the 32 hex digits of a uuid4."""
    return "artifact-" + uuid4().hex


def is_backend_artifact_id(value: object) -> bool:
    """True only for a string in the backend id format."""
    return isinstance(value, str) and BACKEND_ARTIFACT_ID_RE.match(value) is not None


__all__ = ["BACKEND_ARTIFACT_ID_RE", "is_backend_artifact_id", "new_backend_artifact_id"]
