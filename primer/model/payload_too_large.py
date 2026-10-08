"""The typed refusal of a request payload over a size cap (FS-04, FS-05).

Its own leaf module so :mod:`primer.model.except_` (a CRLF file) stays
untouched, the same way :mod:`primer.model.workspace_refusal` does it.
"""

from __future__ import annotations

from primer.model.except_ import PrimerError


class PayloadTooLargeError(PrimerError):
    """A request body, an uploaded archive, or what it inflates to is over a cap.

    Maps to HTTP 413 with the ``/errors/payload-too-large`` problem type.
    Exactly one of ``limit_bytes`` (a byte cap) or ``limit_entries`` (a cap
    on the number of archive entries) names the cap that was exceeded.
    """

    def __init__(
        self,
        message: str,
        *,
        limit_bytes: int | None = None,
        limit_entries: int | None = None,
    ) -> None:
        super().__init__(message)
        self.limit_bytes = limit_bytes
        self.limit_entries = limit_entries

    @property
    def problem_extensions(self) -> dict[str, object]:
        """The machine-readable code and the cap, matching the body-limit middleware's 413."""
        extensions: dict[str, object] = {"code": "payload_too_large"}
        if self.limit_bytes is not None:
            extensions["limit_bytes"] = self.limit_bytes
        if self.limit_entries is not None:
            extensions["limit_entries"] = self.limit_entries
        return extensions


__all__ = ["PayloadTooLargeError"]
