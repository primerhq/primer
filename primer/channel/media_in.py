"""Inbound channel media: land attachments as workspace files.

S6 section 6: an inbound attachment is written into the target session's
workspace under ``media/<fire_id>/<filename>`` and the steer text references
the path. Bytes reach here either inline on the part (small captures) or
behind an ``artifact_id`` the platform adapter already stored.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MEDIA_INBOX_ROOT = "media"


def _safe_name(name: str | None, index: int) -> str:
    """Reduce a platform-supplied filename to a single safe path segment."""
    base = os.path.basename((name or "").strip()) if name else ""
    base = base.replace("\x00", "")
    if not base or base in {".", ".."}:
        return f"attachment-{index}"
    return base


async def _part_bytes(part, artifact_storage) -> bytes | None:
    """Inline bytes if present, else the referenced artifact's bytes."""
    data = getattr(part, "data", None)
    if data:
        return data
    artifact_id = getattr(part, "artifact_id", None)
    if not artifact_id or artifact_storage is None:
        return None
    blob = await artifact_storage.get(artifact_id)
    return getattr(blob, "data", None) if blob is not None else None


async def land_media_in_workspace(
    *,
    workspace,
    fire_id: str,
    parts: list | None,
    artifact_storage,
) -> list[str]:
    """Write each media part under ``media/<fire_id>/`` and return the paths.

    Best-effort per part: an unresolvable or unwritable attachment is
    skipped with a log rather than failing the whole inbound message.
    """
    written: list[str] = []
    for index, part in enumerate(parts or []):
        data = await _part_bytes(part, artifact_storage)
        if not data:
            logger.warning(
                "media_in: attachment %d of fire %s has no bytes; skipping",
                index, fire_id,
            )
            continue
        path = (
            f"{MEDIA_INBOX_ROOT}/{fire_id}/"
            f"{_safe_name(getattr(part, 'filename', None), index)}"
        )
        try:
            await workspace.write_file(path, data)
        except Exception:  # noqa: BLE001 - one bad file must not drop the turn
            logger.warning(
                "media_in: writing %s failed; skipping", path, exc_info=True,
            )
            continue
        written.append(path)
    return written


def compose_steer_text(
    text: str | None,
    paths: list[str],
    *,
    transcript: str | None = None,
    note: str | None = None,
) -> str:
    """Build the steer text: body, then attachment paths, then any note.

    A transcript takes the body position (S6 section 6: the transcript IS
    the steer text), with the original file still referenced below it.
    """
    body = (transcript or text or "").strip()
    lines: list[str] = [body] if body else []
    if paths:
        lines.append(
            "Attachments in this workspace:\n"
            + "\n".join(f"- {p}" for p in paths)
        )
    if note:
        lines.append(f"({note})")
    return "\n\n".join(lines)


__all__ = [
    "MEDIA_INBOX_ROOT",
    "compose_steer_text",
    "land_media_in_workspace",
]
