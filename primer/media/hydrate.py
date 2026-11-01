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

import asyncio
import logging

from primer.int.artifact_storage import ArtifactStorage
from primer.model.chat import Message, Part, TextPart


logger = logging.getLogger(__name__)

# How many artifacts one prompt fetches at a time.
_FETCH_CONCURRENCY = 8


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
    # Every distinct artifact the prompt names is fetched once, all at once (bounded), instead of one after another per part (L5). A part
    # whose artifact is not found, and that has no url or file_id either, is replaced in this copy by a note naming its type (GX-8).
    wanted = list(dict.fromkeys(
        aid
        for msg in messages
        for p in msg.parts
        if (aid := getattr(p, "artifact_id", None)) and not getattr(p, "data", None)
    ))
    blobs: dict[str, object] = {}
    if wanted:
        gate = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch(aid: str) -> None:
            async with gate:
                blobs[aid] = await artifact_storage.get(aid)

        await asyncio.gather(*(fetch(aid) for aid in wanted))
    out: list[Message] = []
    for msg in messages:
        if not any(getattr(p, "artifact_id", None) for p in msg.parts):
            out.append(msg)
            continue
        hydrated_parts = [_with_blob(p, blobs) for p in msg.parts]
        out.append(msg.model_copy(update={"parts": hydrated_parts}))
    return out


def _with_blob(part: Part, blobs: dict[str, object]) -> Part:
    aid = getattr(part, "artifact_id", None)
    if not aid or getattr(part, "data", None):
        return part
    blob = blobs.get(aid)
    if blob is None:
        logger.warning("hydrate_part: artifact %s not found", aid)
        if getattr(part, "url", None) or getattr(part, "file_id", None):
            return part
        # A part with no source left is refused by the provider, on this turn and every later one of the session (GX-8): tell the model instead.
        return TextPart(text=f"[attachment unavailable: {getattr(part, 'mime_type', None) or part.type}]")
    update: dict = {"data": blob.data, "artifact_id": None}  # type: ignore[attr-defined]
    if getattr(part, "mime_type", None) is None:
        update["mime_type"] = blob.mime_type  # type: ignore[attr-defined]
    return part.model_copy(update=update)


__all__ = ["hydrate_part", "hydrate_prompt_parts"]
