"""S6 P4: a voice note transcribes when an STT provider is configured.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 6. The active-STT
resolution is S4's (06-s4-design.md section 3); this suite injects a stub so
the fork is pinned independently of S4's shipping state.
"""

from __future__ import annotations

from primer.channel.media_in import (
    NO_STT_NOTE,
    is_voice_part,
    resolve_active_stt,
    transcribe_voice_parts,
)


class _Part:
    def __init__(self, mime_type=None, filename=None, data=None) -> None:
        self.mime_type = mime_type
        self.filename = filename
        self.data = data
        self.artifact_id = None


class _StubSTT:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def transcribe(self, data: bytes, filename: str) -> str:
        self.calls.append(filename)
        return "please summarise the deck"


class _BrokenSTT:
    async def transcribe(self, data: bytes, filename: str) -> str:
        raise RuntimeError("provider down")


def test_audio_parts_are_voice_parts():
    assert is_voice_part(_Part(mime_type="audio/ogg")) is True
    assert is_voice_part(_Part(mime_type="image/png")) is False
    assert is_voice_part(_Part()) is False


async def test_transcript_is_returned_when_stt_is_configured():
    stt = _StubSTT()
    transcript, note = await transcribe_voice_parts(
        parts=[_Part(mime_type="audio/ogg", filename="n.ogg", data=b"OggS")],
        artifact_storage=None,
        stt=stt,
    )
    assert transcript == "please summarise the deck"
    assert note is None
    assert stt.calls == ["n.ogg"]


async def test_no_stt_attaches_as_is_with_a_note():
    transcript, note = await transcribe_voice_parts(
        parts=[_Part(mime_type="audio/ogg", filename="n.ogg", data=b"OggS")],
        artifact_storage=None,
        stt=None,
    )
    assert transcript is None
    assert note == NO_STT_NOTE


async def test_a_failing_provider_degrades_to_the_note():
    transcript, note = await transcribe_voice_parts(
        parts=[_Part(mime_type="audio/ogg", filename="n.ogg", data=b"OggS")],
        artifact_storage=None,
        stt=_BrokenSTT(),
    )
    assert transcript is None
    assert note is not None


async def test_no_audio_means_no_fork_at_all():
    transcript, note = await transcribe_voice_parts(
        parts=[_Part(mime_type="image/png", filename="a.png", data=b"PNG")],
        artifact_storage=None,
        stt=_StubSTT(),
    )
    assert transcript is None
    assert note is None


async def test_resolver_returns_none_without_a_configured_provider():
    class _EmptyStorage:
        async def get(self, _id):
            return None

    class _SP:
        def get_storage(self, _cls):
            return _EmptyStorage()

    assert await resolve_active_stt(_SP()) is None
