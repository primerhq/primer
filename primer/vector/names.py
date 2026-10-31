"""One collision-free vector table name per collection id, for every backend (review of knowledge, finding 10).

``team-notes`` and ``team_notes`` used to become the same table (and ids with a dot could not be indexed). The name is ``embeddings_<slug>_<hash>``:
a short slug of the id (anything outside ``[A-Za-z0-9]`` becomes ``_``) plus the first 10 hex digits of the SHA-256 of the EXACT id, so two ids that
sanitise alike still differ. At most 52 characters: Postgres truncates identifiers at 63 bytes and the table's own index names add up to 9.

Collections created before this keep the table their catalogue row stored (every read goes through the catalogue); only a NEW collection gets
this name.
"""

from __future__ import annotations

import hashlib
import re

from primer.model.except_ import BadRequestError

COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SLUG_MAX = 30


def table_name_for_collection(collection_id: str) -> str:
    """The vector table name of ``collection_id``; ``BadRequestError`` for an id outside ``[A-Za-z0-9._-]``."""
    if not COLLECTION_ID_RE.match(collection_id):
        raise BadRequestError(
            f"collection_id {collection_id!r} must match {COLLECTION_ID_RE.pattern} (alphanumeric / dot / underscore / hyphen)"
        )
    slug = re.sub(r"[^A-Za-z0-9]+", "_", collection_id)[:_SLUG_MAX].strip("_") or "c"
    digest = hashlib.sha256(collection_id.encode("utf-8")).hexdigest()[:10]
    return f"embeddings_{slug}_{digest}"


__all__ = ["COLLECTION_ID_RE", "table_name_for_collection"]
