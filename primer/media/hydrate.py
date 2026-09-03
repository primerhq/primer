"""Resolve artifact-backed chat parts to inline bytes.

Split out of ``primer.channel.media`` (which re-exports these two names for
backward compatibility): the logic here touches only ``Message``/``Part``
and the ``ArtifactStorage`` interface, so it belongs below ``primer.channel``
in the dependency graph rather than behind it -- the agent turn loop needs
it too, and agent/worker may not import primer.channel (see the
"agent/worker reach channel only through sanctioned dispatch seams"
import-linter contract).
"""

from __future__ import annotations

import logging

from primer.int.artifact_storage import ArtifactStorage
from primer.model.chat import Message, Part


logger = logging.getLogger(__name__)


async def hydrate_part(artifact_storage: ArtifactStorage, part: Part) -> Part:
    """Return a copy of ``part`` with inline ``data`` populated from its
    ``artifact_id`` (when set and ``data`` is empty). Clears ``artifact_id`` on
    the copy so downstream consumers see only ``data``. Parts without an
    ``artifact_id`` (or that already carry ``data``) pass through unchanged."""
    aid = getattr(part, "artifact_id", None)
    if not aid or getattr(part, "data", None):
        return part
    blob = await artifact_storage.get(aid)
    if blob is None:
        logger.warning("hydrate_part: artifact %s not found", aid)
        return part
    update: dict = {"data": blob.data, "artifact_id": None}
    if getattr(part, "mime_type", None) is None:
        update["mime_type"] = blob.mime_type
    return part.model_copy(update=update)


async def hydrate_prompt_parts(
    artifact_storage: ArtifactStorage | None, messages: list[Message],
) -> list[Message]:
    """Resolve every part's ``artifact_id`` to inline ``data`` across a
    whole prompt, so an LLM adapter (which only reads ``data``/``url``/
    ``file_id``, never ``artifact_id``) sees real bytes for any
    artifact-backed part in history or the turn's new messages.

    ``artifact_storage=None`` returns ``messages`` unchanged (no-op) --
    callers that never resolve a store (tests, surfaces with no
    ArtifactStorage wired) keep today's behaviour exactly. Messages with
    no binary parts are returned as the SAME object (no copy), so this is
    cheap to call unconditionally on every turn.
    """
    if artifact_storage is None:
        return messages
    out: list[Message] = []
    for msg in messages:
        if not any(getattr(p, "artifact_id", None) for p in msg.parts):
            out.append(msg)
            continue
        hydrated_parts = [
            await hydrate_part(artifact_storage, p) for p in msg.parts
        ]
        out.append(msg.model_copy(update={"parts": hydrated_parts}))
    return out


__all__ = ["hydrate_part", "hydrate_prompt_parts"]
