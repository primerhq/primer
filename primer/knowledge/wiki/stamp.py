"""The wiki version stamp: which build may replace the stored wiki.

Every pod of every build regenerates the wiki when it boots, and a rolling deploy runs old and
new pods side by side. The stamp (one :class:`~primer.model.wiki.WikiStamp` row) records which
build wrote the wiki last, and a build writes only when :func:`decide` says so.

The rule, from the plan's decision 3. The app version cannot decide alone: releases are cut by
hand and every k3s deploy carries the same version, and a content hash has no order. So:

* a higher app version writes, a lower one never does;
* at the same app version a build writes unless its hash is the stored one (nothing to do) or one
  the stamp already replaced (``superseded``, the last 20). A pod of a build that was replaced
  never writes again, which is what stops a rolling deploy from flipping pages back.

Known limit: rolling back to an earlier build of the same app version keeps the later wiki until
a new build ships.

:func:`claim` is the write that makes a build the stored one, a compare-and-set on the hash it
read, so of any number of pods racing to claim exactly one wins each step. :func:`still_mine`
lets the winner check, between page writes, that no newer claim replaced it.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from primer.model.except_ import ConflictError, NotFoundError
from primer.model.wiki import WIKI_STAMP_ID, WikiStamp

SUPERSEDED_KEEP = 20

Decision = Literal["write", "same", "older", "superseded"]

_CLAIM_ATTEMPTS = 3
"""Reads a claim makes before giving up: one more only when a peer's create or a delete moved the row."""

_LEADING_DIGITS = re.compile(r"\d+")


def version_key(app_version: str) -> tuple[int, ...]:
    """The release part of a version (before ``+``) as integers: each ``.`` segment's leading digits, ``0`` when none."""
    return tuple(
        int(match.group()) if (match := _LEADING_DIGITS.match(segment)) else 0
        for segment in app_version.split("+", 1)[0].split(".")
    )


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """-1, 0 or 1; the shorter key is padded with zeros, so ``0.6`` and ``0.6.0`` are the same version."""
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return (left > right) - (left < right)


def decide(stored: WikiStamp | None, *, app_version: str, content_hash: str) -> Decision:
    """May a build with this app version and content hash write the wiki over the stored stamp?"""
    if stored is None:
        return "write"
    order = _compare(version_key(app_version), version_key(stored.app_version))
    if order > 0:
        return "write"
    if order < 0:
        return "older"
    if content_hash == stored.content_hash:
        return "same"
    if content_hash in stored.superseded:
        return "superseded"
    return "write"


async def read_stamp(storage_provider) -> WikiStamp | None:
    """The stored stamp, or ``None`` when no build has claimed the wiki yet."""
    return await storage_provider.get_storage(WikiStamp).get(WIKI_STAMP_ID)


def _replaced_by(stored: WikiStamp, content_hash: str) -> list[str]:
    """The ``superseded`` list after ``content_hash`` replaces ``stored``: the old hash first, never the new one."""
    replaced = [] if stored.content_hash == content_hash else [stored.content_hash]
    older = [h for h in stored.superseded if h not in (stored.content_hash, content_hash)]
    return [*replaced, *older][:SUPERSEDED_KEEP]


async def claim(storage_provider, *, app_version: str, content_hash: str) -> bool:
    """Make this build the stored one. True when it did; False when it may not or lost the race.

    No stamp yet: ``create`` it; a ``ConflictError`` means a peer created it first, so the stamp
    is read again and the decision made once more. A stamp: one ``patch_if`` of the four fields,
    guarded on the hash that was read, so a peer that replaced the stamp in between makes this
    write a no-op (False). The hash it replaces goes to the front of ``superseded``.
    """
    stamps = storage_provider.get_storage(WikiStamp)
    for _ in range(_CLAIM_ATTEMPTS):
        stored = await stamps.get(WIKI_STAMP_ID)
        if decide(stored, app_version=app_version, content_hash=content_hash) != "write":
            return False
        superseded = [] if stored is None else _replaced_by(stored, content_hash)
        new = WikiStamp(
            id=WIKI_STAMP_ID,
            app_version=app_version,
            content_hash=content_hash,
            superseded=superseded,
            written_at=datetime.now(UTC),
        )
        if stored is None:
            try:
                await stamps.create(new)
            except ConflictError:
                continue
            return True
        patch = new.model_dump(mode="json", include={"app_version", "content_hash", "superseded", "written_at"})
        try:
            written = await stamps.patch_if(WIKI_STAMP_ID, patch, where={"content_hash": [stored.content_hash]})
        except NotFoundError:
            continue
        return written is not None
    return False


async def still_mine(storage_provider, content_hash: str) -> bool:
    """Is the stored stamp this build's? False once a newer claim replaced it (or when there is none)."""
    stored = await read_stamp(storage_provider)
    return stored is not None and stored.content_hash == content_hash


__all__ = [
    "SUPERSEDED_KEEP",
    "Decision",
    "claim",
    "decide",
    "read_stamp",
    "still_mine",
    "version_key",
]
