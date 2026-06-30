"""Tests for primer.tap.cursor — TapCursor opaque per-session seq-vector."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from primer.tap.cursor import TapCursor

FIXED_TS = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# resume_seq
# ---------------------------------------------------------------------------


class TestResumeSeq:
    def test_unknown_session_returns_zero(self) -> None:
        cursor = TapCursor(seqs={}, known_as_of=FIXED_TS)
        assert cursor.resume_seq("never-seen") == 0

    def test_known_session_returns_stored_value(self) -> None:
        cursor = TapCursor(seqs={"sess-1": 42}, known_as_of=FIXED_TS)
        assert cursor.resume_seq("sess-1") == 42


# ---------------------------------------------------------------------------
# advance
# ---------------------------------------------------------------------------


class TestAdvance:
    def test_advance_sets_seq_for_new_session(self) -> None:
        cursor = TapCursor(seqs={}, known_as_of=FIXED_TS)
        cursor.advance("sess-1", 10)
        assert cursor.resume_seq("sess-1") == 10

    def test_advance_bumps_existing_session(self) -> None:
        cursor = TapCursor(seqs={"sess-1": 5}, known_as_of=FIXED_TS)
        cursor.advance("sess-1", 20)
        assert cursor.resume_seq("sess-1") == 20

    def test_advance_with_lower_seq_does_not_regress(self) -> None:
        cursor = TapCursor(seqs={"sess-1": 50}, known_as_of=FIXED_TS)
        cursor.advance("sess-1", 10)
        assert cursor.resume_seq("sess-1") == 50

    def test_advance_same_seq_is_idempotent(self) -> None:
        cursor = TapCursor(seqs={"sess-1": 7}, known_as_of=FIXED_TS)
        cursor.advance("sess-1", 7)
        assert cursor.resume_seq("sess-1") == 7


# ---------------------------------------------------------------------------
# prune_ended
# ---------------------------------------------------------------------------


class TestPruneEnded:
    def test_prune_drops_ended_sessions(self) -> None:
        cursor = TapCursor(
            seqs={"sess-a": 1, "sess-b": 2, "sess-c": 3}, known_as_of=FIXED_TS
        )
        cursor.prune_ended({"sess-a", "sess-c"})
        assert cursor.resume_seq("sess-a") == 0
        assert cursor.resume_seq("sess-c") == 0

    def test_prune_keeps_remaining_sessions(self) -> None:
        cursor = TapCursor(
            seqs={"sess-a": 1, "sess-b": 2, "sess-c": 3}, known_as_of=FIXED_TS
        )
        cursor.prune_ended({"sess-a", "sess-c"})
        assert cursor.resume_seq("sess-b") == 2

    def test_prune_unknown_ids_is_safe(self) -> None:
        cursor = TapCursor(seqs={"sess-a": 1}, known_as_of=FIXED_TS)
        cursor.prune_ended({"nonexistent"})
        assert cursor.resume_seq("sess-a") == 1

    def test_prune_empty_set_is_noop(self) -> None:
        cursor = TapCursor(seqs={"sess-a": 1}, known_as_of=FIXED_TS)
        cursor.prune_ended(set())
        assert cursor.resume_seq("sess-a") == 1


# ---------------------------------------------------------------------------
# encode / decode round-trip
# ---------------------------------------------------------------------------


class TestEncodeDecodeRoundtrip:
    def test_roundtrip_preserves_seqs(self) -> None:
        original = TapCursor(seqs={"s1": 3, "s2": 99}, known_as_of=FIXED_TS)
        token = original.encode()
        restored = TapCursor.decode(token)
        assert restored.seqs == original.seqs

    def test_roundtrip_preserves_known_as_of(self) -> None:
        original = TapCursor(seqs={"s1": 1}, known_as_of=FIXED_TS)
        token = original.encode()
        restored = TapCursor.decode(token)
        assert restored.known_as_of == original.known_as_of

    def test_roundtrip_empty_seqs(self) -> None:
        original = TapCursor(seqs={}, known_as_of=FIXED_TS)
        token = original.encode()
        restored = TapCursor.decode(token)
        assert restored.seqs == {}

    def test_token_is_url_safe(self) -> None:
        cursor = TapCursor(seqs={"sess": 123}, known_as_of=FIXED_TS)
        token = cursor.encode()
        # No +, /, =, or whitespace — only base64url alphabet + possible dots
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", token), (
            f"Token is not URL-safe: {token!r}"
        )


# ---------------------------------------------------------------------------
# decode tolerance — None, empty, garbage
# ---------------------------------------------------------------------------


class TestDecodeTolerance:
    def test_decode_none_returns_empty_cursor(self) -> None:
        cursor = TapCursor.decode(None)
        assert cursor.seqs == {}

    def test_decode_empty_string_returns_empty_cursor(self) -> None:
        cursor = TapCursor.decode("")
        assert cursor.seqs == {}

    def test_decode_garbage_returns_empty_cursor(self) -> None:
        cursor = TapCursor.decode("!!!not-base64!!!")
        assert cursor.seqs == {}

    def test_decode_truncated_base64_returns_empty_cursor(self) -> None:
        cursor = TapCursor.decode("YQ")  # valid b64 but not valid JSON
        assert cursor.seqs == {}

    def test_decode_does_not_raise_on_bad_input(self) -> None:
        for bad in [None, "", "!!!not-base64!!!", "YQ", "       "]:
            result = TapCursor.decode(bad)
            assert isinstance(result, TapCursor)
