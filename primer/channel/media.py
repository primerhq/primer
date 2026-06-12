"""Provider-agnostic media plumbing for channel chats.

No platform SDKs here. This module:

* maps a MIME type to the right chat :class:`Part` class,
* compresses images before storage (best-effort),
* enforces size + type limits,
* stores inbound bytes as an artifact and builds the referencing part,
* collects media parts from a turn's output (outbound), and
* hydrates an artifact-referencing part back to inline ``data`` (for the LLM
  at turn time and for a channel upload at relay time).

Bytes live in the :class:`primer.int.artifact_storage.ArtifactStorage` backend;
persisted parts carry only an ``artifact_id`` reference.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from primer.int.artifact_storage import ArtifactStorage
from primer.model.chat import (
    AudioPart, DocumentPart, ImagePart, Part, VideoPart,
)


logger = logging.getLogger(__name__)


class MediaError(Exception):
    """Base for media-handling rejections."""


class MediaTooLarge(MediaError):
    """The attachment exceeds the configured size cap."""


class MediaTypeNotAllowed(MediaError):
    """The attachment's MIME type is outside the configured allowlist."""


@dataclass
class MediaConfig:
    """Tunables for inbound media handling. Defaults are permissive; the
    model + the existing rejection-remediation path handle types a given
    model cannot consume."""

    max_bytes: int = 20 * 1024 * 1024
    image_max_dimension: int = 2048
    image_quality: int = 85
    allowed_prefixes: tuple[str, ...] = ("image/", "audio/", "video/", "text/")
    allowed_exact: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/json",
        })
    )


def part_cls_for_mime(mime_type: str | None) -> type[Part]:
    """Choose the chat Part class for a MIME type. Unknown -> DocumentPart."""
    mt = (mime_type or "").lower()
    if mt.startswith("image/"):
        return ImagePart
    if mt.startswith("audio/"):
        return AudioPart
    if mt.startswith("video/"):
        return VideoPart
    return DocumentPart


def is_allowed(mime_type: str | None, config: MediaConfig) -> bool:
    mt = (mime_type or "").lower()
    if mt in config.allowed_exact:
        return True
    return any(mt.startswith(p) for p in config.allowed_prefixes)


def enforce_limits(*, size: int, mime_type: str | None, config: MediaConfig) -> None:
    """Raise when an attachment is too large or of a disallowed type."""
    if size > config.max_bytes:
        raise MediaTooLarge(
            f"attachment is {size} bytes (cap {config.max_bytes})"
        )
    if not is_allowed(mime_type, config):
        raise MediaTypeNotAllowed(f"MIME type {mime_type!r} is not allowed")


def compress_image(
    data: bytes, mime_type: str | None, config: MediaConfig,
) -> tuple[bytes, str]:
    """Downscale + re-encode an image to keep inline storage small.

    Best-effort: any failure (non-image MIME, format Pillow can't read,
    animated GIF, etc.) returns the original bytes + MIME unchanged.
    """
    mt = (mime_type or "").lower()
    if not mt.startswith("image/"):
        return data, mt or "application/octet-stream"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            has_alpha = img.mode in ("RGBA", "LA", "P") and (
                "transparency" in img.info or img.mode in ("RGBA", "LA")
            )
            longest = max(img.size)
            if longest > config.image_max_dimension:
                scale = config.image_max_dimension / longest
                new_size = (
                    max(1, round(img.size[0] * scale)),
                    max(1, round(img.size[1] * scale)),
                )
                img = img.resize(new_size, Image.LANCZOS)
            out = io.BytesIO()
            if has_alpha:
                img.convert("RGBA").save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png"
            img.convert("RGB").save(
                out, format="JPEG", quality=config.image_quality, optimize=True,
            )
            return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 — best-effort; keep original on any failure
        logger.debug("compress_image: keeping original bytes", exc_info=True)
        return data, mt


async def store_inbound_media(
    artifact_storage: ArtifactStorage,
    *,
    data: bytes,
    mime_type: str | None,
    filename: str | None = None,
    config: MediaConfig | None = None,
) -> Part:
    """Enforce limits, compress images, store the bytes, and return the
    artifact-referencing chat Part. Raises :class:`MediaError` on rejection."""
    cfg = config or MediaConfig()
    enforce_limits(size=len(data), mime_type=mime_type, config=cfg)
    stored, stored_mime = compress_image(data, mime_type, cfg)
    aid = await artifact_storage.put(
        data=stored, mime_type=stored_mime, filename=filename,
    )
    cls = part_cls_for_mime(stored_mime)
    kwargs: dict = {"artifact_id": aid, "mime_type": stored_mime}
    if "filename" in cls.model_fields and filename:
        kwargs["filename"] = filename
    return cls(**kwargs)


_MEDIA_PART_TYPES = (ImagePart, DocumentPart, AudioPart, VideoPart)


def collect_media_parts(parts: list[Part]) -> list[Part]:
    """Return the binary media parts (image/document/audio/video) from a list."""
    return [p for p in parts if isinstance(p, _MEDIA_PART_TYPES)]


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


__all__ = [
    "MediaConfig",
    "MediaError",
    "MediaTooLarge",
    "MediaTypeNotAllowed",
    "collect_media_parts",
    "compress_image",
    "enforce_limits",
    "hydrate_part",
    "is_allowed",
    "part_cls_for_mime",
    "store_inbound_media",
]
