"""Capability URLs for the workspace files a canvas shows (pivot P5, Task 14; plan Decision 12, design 8.6).

An Image or Audio element may show a workspace file (``file:<path>``). The frame is a sandboxed, cross-origin document and sends no
cookies, so it cannot use the authenticated file routes: ``GET .../canvas`` mints one URL per referenced file instead,
``/canvas/sessions/<session id>/files/<token>``, and the URL is the credential (the lead's ruling 6 on S6):

* **unguessable**: the token is ``itsdangerous.URLSafeTimedSerializer`` over ``{"s": session id, "p": path}``, signed with the
  install's session secret under its own salt (:data:`FILE_TOKEN_SALT`), so a token of another purpose never verifies here;
* **scoped to one session's workspace file**: the route serves it only under the session the token names, only while an Image or
  Audio element of that session's canvas references the path (:func:`referenced_files`), and only from that session's workspace;
* **expiring**: a token older than :data:`FILE_URL_TTL_S` (900 s) is refused; the console refetches the canvas for fresh ones;
* **never outside the workspace**: a path with an absolute, empty, ``.`` or ``..`` segment is refused before the workspace is asked
  (:func:`safe_relative_path`), the workspace's own resolver refuses an escape (a symlink included), and a path in its reserved trees
  (``.state``, ``.tmp``, the way the backend resolves it) is refused.

Only the media types of :data:`MEDIA_TYPES` are served, at most :data:`MAX_FILE_BYTES`, with no Range support (v1).
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from itsdangerous import BadData, URLSafeTimedSerializer

FILE_TOKEN_SALT = "primer.canvas.file.v1"
#: How long a minted URL is good for, in seconds.
FILE_URL_TTL_S = 900
#: The largest file a URL serves (25 MiB).
MAX_FILE_BYTES = 26_214_400
#: The media types served, by lowercased extension (the catalog's ``IMAGE_FILE_TYPES`` and ``AUDIO_FILE_TYPES``).
MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "weba": "audio/webm",
}

#: The element types whose ``src`` may name a workspace file.
_FILE_ELEMENTS = ("Image", "Audio")
_FILE_PREFIX = "file:"
#: The longest token :func:`verify_file_token` reads (a minted one is about 100 characters).
_MAX_TOKEN_CHARS = 2048


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=FILE_TOKEN_SALT)


def canvas_file_secret(app_state: Any) -> str:
    """The key file URLs are signed with: the install's session secret; else (auth disabled, so there is none) one random secret kept on
    ``app_state`` for the life of the process. An auth-disabled install runs one API process, so the URLs it mints verify there."""
    secret = getattr(app_state, "session_secret", None)
    if secret:
        return secret
    own = getattr(app_state, "canvas_file_secret", None)
    if not own:
        own = secrets.token_urlsafe(32)
        app_state.canvas_file_secret = own
    return own


def mint_file_url(secret: str, session_id: str, path: str) -> str:
    """The capability URL of the workspace file ``path`` for the canvas of ``session_id``."""
    return f"/canvas/sessions/{session_id}/files/{_serializer(secret).dumps({'s': session_id, 'p': path})}"


def verify_file_token(secret: str, token: str, *, max_age: int = FILE_URL_TTL_S) -> tuple[str, str] | None:
    """``(session id, path)`` of a token this install signed less than ``max_age`` seconds ago, else ``None`` (tampered, made up,
    expired, signed for another purpose, or not of the minted shape)."""
    if not token or len(token) > _MAX_TOKEN_CHARS:
        return None
    try:
        payload = _serializer(secret).loads(token, max_age=max_age)
    except (BadData, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"s", "p"}:
        return None
    session_id, path = payload["s"], payload["p"]
    if not isinstance(session_id, str) or not isinstance(path, str) or not session_id or not path:
        return None
    return session_id, path


def referenced_files(elements: Mapping[str, Any]) -> set[str]:
    """The workspace paths the Image and Audio elements of ``elements`` show (their ``file:`` sources, the scheme matched as the
    catalog matches it, in any case)."""
    paths: set[str] = set()
    for element in elements.values():
        if not isinstance(element, Mapping) or element.get("type") not in _FILE_ELEMENTS:
            continue
        src = (element.get("props") or {}).get("src")
        if isinstance(src, str) and src[: len(_FILE_PREFIX)].lower() == _FILE_PREFIX:
            paths.add(src[len(_FILE_PREFIX):])
    return paths


def safe_relative_path(path: str) -> bool:
    """Whether ``path`` is a plain workspace-relative path: no leading ``/``, no backslash, no control character, and no empty, ``.``
    or ``..`` segment. Asked before the workspace is (its resolver refuses an escape too)."""
    if not path or "\\" in path or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return False
    return all(segment not in ("", ".", "..") for segment in path.split("/"))


def media_type_of(path: str) -> str | None:
    """The served media type of ``path`` by its extension, or ``None`` when it is not one of :data:`MEDIA_TYPES`."""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return MEDIA_TYPES.get(name.rsplit(".", 1)[1].lower())


__all__ = [
    "FILE_TOKEN_SALT",
    "FILE_URL_TTL_S",
    "MAX_FILE_BYTES",
    "MEDIA_TYPES",
    "canvas_file_secret",
    "media_type_of",
    "mint_file_url",
    "referenced_files",
    "safe_relative_path",
    "verify_file_token",
]
