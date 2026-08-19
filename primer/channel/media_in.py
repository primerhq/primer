"""Inbound channel media: land attachments as workspace files.

S6 section 6: an inbound attachment is written into the target session's
workspace under ``media/<fire_id>/<filename>`` and the steer text references
the path. Bytes reach here either inline on the part (small captures) or
behind an ``artifact_id`` the platform adapter already stored.
"""

from __future__ import annotations

import logging
import mimetypes
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


NO_STT_NOTE = (
    "voice note attached as-is: no speech-to-text provider is configured"
)

def is_voice_part(part) -> bool:
    """True for an audio attachment (the transcription candidate)."""
    mime = (getattr(part, "mime_type", None) or "").lower()
    return mime.startswith("audio/")


class _BoundSTT:
    """Adapt S4's ASR to the two-argument call this fork makes.

    :meth:`primer.int.asr.ASR.transcribe` is keyword-only and needs the
    model and mimetype the provider row configures; the media fork only
    knows the bytes and a filename. This binder holds the rest and unwraps
    the ``Transcription | SpeechError`` result to the plain text the fork
    composes into the steer, so an errors-as-values SpeechError reads as
    "no transcript" rather than escaping as an exception.
    """

    def __init__(self, adapter, model: str) -> None:
        self._adapter = adapter
        self._model = model

    async def transcribe(self, data: bytes, filename: str) -> str:
        from primer.model.speech import SpeechError

        result = await self._adapter.transcribe(
            model=self._model,
            audio=data,
            filename=filename,
            mimetype=_guess_mimetype(filename),
        )
        if isinstance(result, SpeechError):
            logger.warning(
                "media_in: STT returned an error: %s",
                getattr(result, "message", result),
            )
            return ""
        return getattr(result, "text", "") or ""


def _guess_mimetype(filename: str) -> str:
    """Best guess for an audio attachment's mimetype."""
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith("audio/"):
        return guessed
    return "audio/ogg"


async def resolve_active_stt(storage_provider, registry=None):
    """Return the configured speech-to-text adapter, or None.

    The provider rows and the ActiveSpeechConfig singleton are owned by S4
    (06-s4-design.md section 3). Every failure to resolve one means "no
    STT", which is the spec's attach-as-is fallback: an install with no
    speech provider is a normal steady state, not a broken bootstrap.
    """
    if registry is not None:
        return registry
    from primer.model.providers.speech import SpeechToTextProvider
    from primer.model.speech import ACTIVE_SPEECH_CONFIG_ID, ActiveSpeechConfig

    try:
        row = await storage_provider.get_storage(ActiveSpeechConfig).get(
            ACTIVE_SPEECH_CONFIG_ID
        )
    except Exception:  # noqa: BLE001 - a storage hiccup means "no STT"
        logger.warning("media_in: active speech config read failed",
                       exc_info=True)
        return None
    provider_id = getattr(row, "stt_provider_id", None) if row else None
    if not provider_id:
        return None
    try:
        from primer.api.registries.speech_registry import (
            SpeechRegistry,
            default_stt_factory,
        )

        storage = storage_provider.get_storage(SpeechToTextProvider)
        provider_row = await storage.get(provider_id)
        if provider_row is None:
            return None
        adapter = await SpeechRegistry(
            storage=storage, factory=default_stt_factory, label="stt",
        ).get(provider_id)
        return _BoundSTT(adapter, provider_row.default_model)
    except Exception:  # noqa: BLE001 - degrade to attach-as-is
        logger.warning("media_in: STT adapter build failed", exc_info=True)
        return None


async def transcribe_voice_parts(
    *, parts: list | None, artifact_storage, stt,
) -> tuple[str | None, str | None]:
    """Transcribe the first audio attachment. Returns ``(transcript, note)``.

    ``(None, None)`` when there is nothing to transcribe; ``(None, note)``
    when there is but no usable provider, which is the attach-as-is branch
    of S6 section 6.
    """
    voice = [p for p in (parts or []) if is_voice_part(p)]
    if not voice:
        return None, None
    if stt is None:
        return None, NO_STT_NOTE
    part = voice[0]
    data = await _part_bytes(part, artifact_storage)
    if not data:
        return None, NO_STT_NOTE
    try:
        transcript = await stt.transcribe(
            data, _safe_name(getattr(part, "filename", None), 0),
        )
    except Exception:  # noqa: BLE001 - never drop the message over ASR
        logger.warning("media_in: transcription failed", exc_info=True)
        return None, "voice note attached as-is: transcription failed"
    text = (transcript or "").strip()
    return (text or None), (None if text else NO_STT_NOTE)


__all__ = [
    "MEDIA_INBOX_ROOT",
    "NO_STT_NOTE",
    "compose_steer_text",
    "is_voice_part",
    "land_media_in_workspace",
    "resolve_active_stt",
    "transcribe_voice_parts",
]
