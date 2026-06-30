"""TapCursor — opaque per-session seq-vector cursor.

The cursor tracks the last-consumed sequence number for each session so that
SSE consumers can resume a tap stream without replaying already-seen events.
It is transmitted as a URL-safe, base64url-encoded (no padding) JSON token
suitable for use in SSE ``Last-Event-ID`` headers and query parameters.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

# Fixed epoch used as the ``known_as_of`` default for empty/undecodable cursors.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TapCursor(BaseModel):
    """Opaque per-session seq-vector cursor.

    ``seqs`` maps session_id to the last-consumed sequence number for that
    session.  ``known_as_of`` records the wall-clock time at which the cursor
    state was captured (injected by the caller for determinism — never derived
    from ``datetime.now()`` in library code paths).
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )

    seqs: dict[str, int]
    known_as_of: datetime

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

    def prune_ended(self, ended_session_ids: set[str]) -> None:
        """Drop cursor entries for sessions that have ended (in-place).

        Keeps the cursor bounded as sessions come and go.  IDs that are not
        present in the cursor are silently ignored.
        """
        for sid in ended_session_ids:
            self.seqs.pop(sid, None)

    # ------------------------------------------------------------------
    # Encode / decode
    # ------------------------------------------------------------------

    def encode(self) -> str:
        """Return a URL-safe, unpadded base64url token representing this cursor.

        The token is safe for SSE ``Last-Event-ID`` headers and query
        parameters (no ``+``, ``/``, ``=``, or whitespace characters).
        """
        payload = {
            "seqs": self.seqs,
            "known_as_of": self.known_as_of.isoformat(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()

    @classmethod
    def decode(cls, token: str | None) -> TapCursor:
        """Decode a cursor token produced by :meth:`encode`.

        Tolerant: ``None``, an empty string, or any undecodable / malformed
        input returns an empty cursor (``seqs={}``) without raising.  This
        allows callers to pass ``Last-Event-ID`` directly without pre-checking.
        """
        if not token or not token.strip():
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
            return cls(seqs=seqs, known_as_of=known_as_of)
        except Exception:  # noqa: BLE001
            return cls(seqs={}, known_as_of=_EPOCH)
