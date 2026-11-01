"""The version stamp of the platform wiki.

One row, id ``system-wiki``, records which wiki build wrote the ``system`` collection last, so
a rolling deploy cannot flip pages back: every pod of every build regenerates the wiki at boot,
and the stamp decides which of them may write (``primer.knowledge.wiki.stamp``).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from primer.model.common import Identifiable

WIKI_STAMP_ID = "system-wiki"


class WikiStamp(Identifiable):
    """Which build holds the wiki: its app version and content hash, and the hashes it replaced."""

    app_version: str = Field(
        ...,
        description="The app version of the build that wrote the wiki (primer.api.version.APP_VERSION).",
    )
    content_hash: str = Field(
        ...,
        description="The content hash of the wiki that build wrote (primer.knowledge.wiki.build.hash_pages).",
    )
    superseded: list[str] = Field(
        default_factory=list,
        description=(
            "The content hashes this stamp replaced, newest first, at most 20. A build whose hash is listed "
            "has already been replaced and never writes the wiki again."
        ),
    )
    source_hash: str | None = Field(
        default=None,
        description=(
            "The hash of the build's SOURCE pages (the hand-written and the legacy ones: no tool catalog, no live toolset), "
            "which only a release changes. A hash in ``superseded`` may write again when the source and the app version are the same: that is a "
            "config toggle within one build (a toolset switched off and on), not a rolling deploy. None on a stamp from before the field."
        ),
    )
    written_at: datetime = Field(..., description="When the stamp last changed.")
