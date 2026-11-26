"""TapCursor — opaque per-session seq-vector cursor.

The cursor tracks the last-consumed sequence number for each session so that
SSE consumers can resume a tap stream without replaying already-seen events.
It is transmitted as a URL-safe, base64url-encoded (no padding) JSON token
suitable for use in SSE ``Last-Event-ID`` headers and query parameters.

The cursor also carries a per-session **byte offset** (``offsets``) used by
:func:`~primer.tap.reader.read_batch` to skip already-consumed bytes in the
append-only session log.  The offset is a *performance hint only*:
``seq > resume_seq`` remains the authoritative correctness filter, so a stale
or wrong offset causes at most a harmless re-read of some records, never a
skip or duplicate.

Assumption: the log is append-only across its segments (no rewrite or compaction), so
the offsets, which are LOGICAL (into the concatenation of the segments, the bytes the
single file would hold), recorded in the cursor remain valid across calls and rotations.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Collection
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

# A ``Last-Event-ID`` / ``?cursor=`` longer than this is not one the SSE stream issued (it keeps its token to a few KB, see ``encode(keep=...)``): the
# SSE route passes it as ``decode(max_chars=...)`` and gets an empty cursor (a fresh, live-from-now connect) rather than parsing it. The MCP drain's
# token is not capped either, but it is bounded: ``read_batch`` drops the ENDED sessions a complete drain fully drained, and the ``drained_as_of``
# watermark it sets makes the drop cheap (a dropped session is skipped, not re-read from byte 0, until it is reopened).
MAX_TOKEN_CHARS = 32 * 1024

# Fixed epoch used as the ``known_as_of`` default for empty/undecodable cursors.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TapCursor(BaseModel):
    """Opaque per-session seq-vector cursor.

    ``seqs`` maps session_id to the last-consumed sequence number for that
    session.  ``known_as_of`` records the wall-clock time at which the cursor
    state was captured (injected by the caller for determinism — never derived
    from ``datetime.now()`` in library code paths).

    ``offsets`` maps session_id to the byte offset *just past* the last
    complete line consumed from that session's ``messages.jsonl``.  It is a
    performance hint: the drain path seeks to this offset to avoid re-reading
    already-consumed bytes, but ``seq > resume_seq`` remains the authoritative
    filter.  A missing entry (offset 0) is always safe.

    ``drained_as_of`` is the MCP drain's watermark: the time of the newest
    WORKSPACE-WIDE drain (no ``sessions`` predicate) that read every session
    to the end (set by :func:`~primer.tap.reader.read_batch` from the caller's
    clock, never derived from ``datetime.now()``).  A scoped drain never moves
    it, so a later, broader drain cannot skip the sessions the scoped one
    never read.  The ENDED sessions such a drain fully drained are dropped
    from ``seqs``/``offsets`` (the token stays bounded), and a drain skips a
    session that is absent, ENDED and ended at or before the watermark --
    ENDED is terminal, so it holds nothing new; a reopen (``ENDED -> CREATED``)
    clears ``ended_at`` and the status, so a reopened session is read again.
    Tokens from before the field decode to the fixed epoch and skip nothing.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )

    seqs: dict[str, int]
    known_as_of: datetime
    offsets: dict[str, int] = {}
    drained_as_of: datetime = _EPOCH

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def resume_seq(self, session_id: str) -> int:
        """Return the last-consumed seq for *session_id*, or ``0`` if unseen.

        A return value of ``0`` means the consumer has never seen this session
        and should replay from the very beginning (seq 1 is the first real
        event).
        """
        return self.seqs.get(session_id, 0)

    def resume_offset(self, session_id: str) -> int:
        """Return the byte offset hint for *session_id*, or ``0`` if absent.

        The offset is the position just past the last complete line consumed
        from ``messages.jsonl``.  Seeking to this offset skips already-read
        bytes; ``seq > resume_seq`` backstops correctness if the offset is
        stale or wrong.
        """
        return self.offsets.get(session_id, 0)

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def advance(self, session_id: str, seq: int) -> None:
        """Advance the cursor for *session_id* to *seq* (in-place, max-wins).

        Advancing with a lower seq than the current value is a no-op so that
        out-of-order delivery cannot regress the cursor.
        """
        current = self.seqs.get(session_id, 0)
        if seq > current:
            self.seqs[session_id] = seq

    def advance_offset(self, session_id: str, offset: int) -> None:
        """Advance the byte-offset hint for *session_id* (in-place, max-wins).

        Max-wins semantics mirror :meth:`advance`: passing a smaller offset
        than the stored value is a no-op, keeping the hint monotonically
        increasing.
        """
        current = self.offsets.get(session_id, 0)
        if offset > current:
            self.offsets[session_id] = offset

    def prune_ended(self, ended_session_ids: set[str]) -> None:
        """Drop cursor entries for sessions that have ended (in-place).

        Drops both the seq entry and the byte-offset hint for each ended
        session, keeping the cursor bounded as sessions come and go.  IDs that
        are not present in the cursor are silently ignored.
        """
        for sid in ended_session_ids:
            self.seqs.pop(sid, None)
            self.offsets.pop(sid, None)

    # ------------------------------------------------------------------
    # Encode / decode
    # ------------------------------------------------------------------

    def encode(self, keep: Collection[str] | None = None) -> str:
        """Return a URL-safe, unpadded base64url token representing this cursor.

        The token is safe for SSE ``Last-Event-ID`` headers and query
        parameters (no ``+``, ``/``, ``=``, or whitespace characters).

        The encoded payload includes ``offsets`` and ``drained_as_of`` alongside ``seqs`` and
        ``known_as_of``; older decoders that do not recognise them will ignore the fields and fall
        back to seq-only resume (harmless re-read; a watermark-less token skips nothing).

        ``keep`` bounds the token: only those sessions' entries are written (the live SSE stream passes its most recently active sessions so a
        long-lived stream's ``Last-Event-ID`` does not grow with every session it ever saw). A session left out is replayed from ``known_as_of``
        by a client that reconnects with the token (at-least-once; the client dedupes by ``(session_id, seq)``). ``None`` writes every entry.
        """
        seqs, offsets = self.seqs, self.offsets
        if keep is not None:
            # Iterate keep (the 64 newest), not the whole map (every session the caller has seen): the token
            # is rebuilt per frame on a long-lived stream.
            seqs = {k: seqs[k] for k in keep if k in seqs}
            offsets = {k: offsets[k] for k in keep if k in offsets}
        payload = {
            "seqs": seqs,
            "known_as_of": self.known_as_of.isoformat(),
            "offsets": offsets,
            "drained_as_of": self.drained_as_of.isoformat(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()

    @classmethod
    def decode(cls, token: str | None, *, max_chars: int | None = None) -> TapCursor:
        """Decode a cursor token produced by :meth:`encode`.

        Tolerant: ``None``, an empty string, or any undecodable / malformed
        input returns an empty cursor (``seqs={}``, ``offsets={}``) without
        raising; so does one longer than ``max_chars`` when that is given.  This allows callers to pass ``Last-Event-ID`` directly
        without pre-checking.

        Backward-compatible: a token that does not contain ``offsets`` (e.g.
        produced by an older encoder) decodes with ``offsets={}`` so the drain
        falls back to seq-only resume with a full re-read from byte 0.
        """
        if not token or not token.strip() or (max_chars is not None and len(token) > max_chars):
            return cls(seqs={}, known_as_of=_EPOCH)
        try:
            # Restore stripped padding before decoding.
            padding = (4 - len(token) % 4) % 4
            raw = base64.urlsafe_b64decode(token + "=" * padding)
            data = json.loads(raw)
            seqs: dict[str, int] = {
                str(k): int(v) for k, v in data["seqs"].items()
            }
            known_as_of = datetime.fromisoformat(data["known_as_of"])
            # ``offsets`` and ``drained_as_of`` are optional -- older tokens omit them; default to {} / the epoch.
            raw_offsets = data.get("offsets", {})
            offsets: dict[str, int] = {
                str(k): int(v) for k, v in raw_offsets.items()
            }
            raw_drained = data.get("drained_as_of")
            drained_as_of = (
                datetime.fromisoformat(raw_drained) if raw_drained is not None else _EPOCH
            )
            return cls(
                seqs=seqs, known_as_of=known_as_of, offsets=offsets, drained_as_of=drained_as_of,
            )
        except Exception:  # noqa: BLE001
            return cls(seqs={}, known_as_of=_EPOCH)
