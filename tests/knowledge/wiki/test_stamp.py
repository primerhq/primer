"""The wiki version stamp: which build may replace the stored wiki.

Pivot P1 task 10. A rolling deploy runs old and new pods at once and they all regenerate the
wiki at boot; the stamp is what stops an old pod from putting the old wiki back. The decision
table is pure; ``claim`` is the compare-and-set that makes one build the stored one, exercised
here on the real SQLite storage (the Postgres twin is ``tests/storage/test_wiki_stamp_postgres.py``).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from primer.knowledge.wiki.stamp import (
    SUPERSEDED_KEEP,
    claim,
    decide,
    read_stamp,
    still_mine,
    version_key,
)
from primer.model.except_ import ConflictError
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.model.storage import OffsetPage
from primer.model.wiki import WIKI_STAMP_ID, WikiStamp
from primer.storage.factory import StorageProviderFactory

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def S(version, digest, superseded=()):
    return WikiStamp(
        id=WIKI_STAMP_ID, app_version=version, content_hash=digest, superseded=list(superseded), written_at=NOW,
    )


@pytest_asyncio.fixture
async def sp(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


# ---- the model --------------------------------------------------------------------


def test_the_stamp_row_has_a_fixed_id_and_an_empty_superseded_list():
    assert WIKI_STAMP_ID == "system-wiki"
    stamp = WikiStamp(id=WIKI_STAMP_ID, app_version="0.6.0", content_hash="a", written_at=NOW)
    assert stamp.superseded == []
    assert SUPERSEDED_KEEP == 20


# ---- version_key ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "version, key",
    [
        ("0.6.0", (0, 6, 0)),
        ("0.0.0+dev", (0, 0, 0)),
        ("10.0.1+build.5", (10, 0, 1)),
        ("1.2.3rc1", (1, 2, 3)),
        ("1.2.dev", (1, 2, 0)),
        ("1", (1,)),
        ("", (0,)),
        ("v1.2", (0, 2)),
    ],
)
def test_version_key(version, key):
    assert version_key(version) == key


# ---- decide ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored, version, digest, expected",
    [
        (None, "0.6.0", "a", "write"),
        (S("0.6.0", "a"), "0.7.0", "a", "write"),
        (S("0.7.0", "b"), "0.6.0", "a", "older"),
        (S("0.6.0", "a"), "0.6.0", "a", "same"),
        (S("0.6.0", "b", ["a"]), "0.6.0", "a", "superseded"),
        (S("0.6.0", "b", ["a"]), "0.6.0", "c", "write"),
        (S("0.0.0+dev", "a"), "0.6.0", "b", "write"),
    ],
)
def test_decide(stored, version, digest, expected):
    assert decide(stored, app_version=version, content_hash=digest) == expected


@pytest.mark.parametrize(
    "stored_version, version, expected",
    [
        ("0.9.0", "0.10.0", "write"),     # numbers, not text: 10 is more than 9
        ("0.10.0", "0.9.0", "older"),
        ("0.6", "0.6.0", "same"),          # a missing segment is a zero
        ("0.6.0", "0.6", "same"),
        ("0.6.0", "0.6.0.1", "write"),
        ("0.6.0+dev", "0.6.0", "same"),    # the local part is ignored
    ],
)
def test_decide_compares_versions_segment_by_segment(stored_version, version, expected):
    assert decide(S(stored_version, "a"), app_version=version, content_hash="a") == expected


def test_a_higher_version_writes_even_a_hash_the_stamp_replaced():
    assert decide(S("0.6.0", "b", ["a"]), app_version="0.7.0", content_hash="a") == "write"


# ---- claim and read_stamp ----------------------------------------------------------------


async def test_no_stamp_until_a_claim(sp):
    assert await read_stamp(sp) is None


async def test_the_first_claim_writes_the_row(sp):
    before = datetime.now(UTC)
    assert await claim(sp, app_version="0.6.0", content_hash="a")
    stamp = await read_stamp(sp)
    assert (stamp.id, stamp.app_version, stamp.content_hash, stamp.superseded) == (WIKI_STAMP_ID, "0.6.0", "a", [])
    assert stamp.written_at.tzinfo is not None and before <= stamp.written_at <= datetime.now(UTC)


async def test_claim_records_the_replaced_hash(sp):
    assert await claim(sp, app_version="0.6.0", content_hash="a")
    assert await claim(sp, app_version="0.6.0", content_hash="b")
    stamp = await read_stamp(sp)
    assert (stamp.content_hash, stamp.superseded) == ("b", ["a"])
    assert not await claim(sp, app_version="0.6.0", content_hash="a")   # a replaced build never claims again


async def test_a_claim_that_is_not_a_write_leaves_the_row_alone(sp):
    assert await claim(sp, app_version="0.7.0", content_hash="b")
    assert await claim(sp, app_version="0.7.0", content_hash="c")
    before = await read_stamp(sp)
    assert before.superseded == ["b"]
    assert not await claim(sp, app_version="0.6.0", content_hash="z")    # older
    assert not await claim(sp, app_version="0.7.0", content_hash="c")    # the stored build
    assert not await claim(sp, app_version="0.7.0", content_hash="b")    # replaced
    assert await read_stamp(sp) == before


async def test_a_newer_version_of_the_same_content_updates_the_version_only(sp):
    assert await claim(sp, app_version="0.6.0", content_hash="a")
    assert await claim(sp, app_version="0.7.0", content_hash="a")
    stamp = await read_stamp(sp)
    assert (stamp.app_version, stamp.content_hash, stamp.superseded) == ("0.7.0", "a", [])


async def test_the_current_hash_is_never_listed_as_superseded(sp):
    assert await claim(sp, app_version="0.6.0", content_hash="a")
    assert await claim(sp, app_version="0.6.0", content_hash="b")
    assert (await read_stamp(sp)).superseded == ["a"]
    assert await claim(sp, app_version="0.7.0", content_hash="a")        # a newer app version may bring old content back
    stamp = await read_stamp(sp)
    assert (stamp.content_hash, stamp.superseded) == ("a", ["b"])


async def test_superseded_keeps_the_last_twenty(sp):
    for index in range(25):
        assert await claim(sp, app_version="0.6.0", content_hash=f"h{index}")
    stamp = await read_stamp(sp)
    assert stamp.content_hash == "h24"
    assert len(stamp.superseded) == 20
    assert stamp.superseded == [f"h{index}" for index in range(23, 3, -1)]      # newest first
    assert not await claim(sp, app_version="0.6.0", content_hash="h10")        # still remembered
    assert await claim(sp, app_version="0.6.0", content_hash="h3")             # fell off the end: it counts as new


async def test_there_is_only_ever_one_stamp_row(sp):
    for digest in ("a", "b", "c"):
        await claim(sp, app_version="0.6.0", content_hash=digest)
    rows = await sp.get_storage(WikiStamp).list(OffsetPage(offset=0, length=10))
    assert [row.id for row in rows.items] == [WIKI_STAMP_ID]


async def test_two_racing_first_claims_leave_one_winner(sp):
    results = await asyncio.gather(
        claim(sp, app_version="0.6.0", content_hash="a"),
        claim(sp, app_version="0.6.0", content_hash="a"),
    )
    assert sorted(results) == [False, True]
    assert (await read_stamp(sp)).content_hash == "a"


async def test_many_pods_of_one_build_leave_one_winner(sp):
    results = await asyncio.gather(*[claim(sp, app_version="0.6.0", content_hash="a") for _ in range(8)])
    assert results.count(True) == 1


async def test_builds_racing_to_replace_one_stamp_lose_no_update(sp):
    # Every winner replaced exactly the stamp it read: none is missing from the chain (Postgres twin:
    # tests/storage/test_wiki_stamp_postgres.py, where the claims really overlap).
    assert await claim(sp, app_version="0.6.0", content_hash="a")
    digests = [f"b{index}" for index in range(6)]
    results = await asyncio.gather(*[claim(sp, app_version="0.6.0", content_hash=d) for d in digests])
    winners = [d for d, won in zip(digests, results, strict=True) if won]
    stamp = await read_stamp(sp)
    assert winners and stamp.content_hash in winners
    assert len(stamp.superseded) == len(winners) and {stamp.content_hash, *stamp.superseded} == {"a", *winners}
    assert await still_mine(sp, stamp.content_hash)
    assert not any([await still_mine(sp, d) for d in digests if d != stamp.content_hash])


async def test_racing_first_claims_of_two_builds_keep_both_hashes_on_the_record(sp):
    # Two different builds can both claim in turn (the second replaces the first); what must hold is
    # that the build that lost the stamp is on the record, so its pods never write again.
    results = await asyncio.gather(
        claim(sp, app_version="0.6.0", content_hash="a"),
        claim(sp, app_version="0.6.0", content_hash="b"),
    )
    assert results == [True, True]
    stamp = await read_stamp(sp)
    assert {stamp.content_hash, *stamp.superseded} == {"a", "b"} and len(stamp.superseded) == 1
    loser = stamp.superseded[0]
    assert not await claim(sp, app_version="0.6.0", content_hash=loser)


# ---- the guard ----------------------------------------------------------------------------


class _Scripted:
    """The real storage provider, except that the stamp storage can be made to meet a race.

    ``reads``: the answers to the first ``get`` calls (a snapshot read earlier, so the row can
    change between a claim's read and its write); later reads are real. ``peer``: a row that
    another pod creates just before this pod's first ``create``, which then loses with a
    ``ConflictError``.
    """

    def __init__(self, real, *, reads=(), peer=None):
        self._real = real
        self._reads = list(reads)
        self._peer = peer

    def get_storage(self, model):
        storage = self._real.get_storage(model)
        if model is not WikiStamp:
            return storage
        outer = self

        class _Stamps:
            def __getattr__(self, name):
                return getattr(storage, name)

            async def get(self, id, **kwargs):  # noqa: A002
                if outer._reads:
                    return outer._reads.pop(0)
                return await storage.get(id, **kwargs)

            async def create(self, entity, **kwargs):
                peer, outer._peer = outer._peer, None
                if peer is not None:
                    await storage.create(peer, **kwargs)
                    raise ConflictError("a peer created the stamp first")
                return await storage.create(entity, **kwargs)

        return _Stamps()


async def test_a_claim_whose_guard_lost_writes_nothing(sp):
    assert await claim(sp, app_version="0.6.0", content_hash="a")
    stale = await read_stamp(sp)                                              # the snapshot a slow pod read
    assert await claim(sp, app_version="0.6.0", content_hash="b")            # another pod got there first
    won = await claim(_Scripted(sp, reads=[stale]), app_version="0.6.0", content_hash="c")
    assert won is False
    stamp = await read_stamp(sp)
    assert (stamp.content_hash, stamp.superseded) == ("b", ["a"])            # nothing of c's landed


async def test_a_claim_retries_when_the_row_vanished_after_it_was_read(sp):
    vanished = S("0.6.0", "a")                                                # read, then the row was deleted
    assert await claim(_Scripted(sp, reads=[vanished]), app_version="0.6.0", content_hash="b") is True
    assert (await read_stamp(sp)).content_hash == "b"


async def test_a_claim_gives_up_rather_than_loop_when_the_row_keeps_vanishing(sp):
    stale = [S("0.6.0", "a")] * 10                                            # every read shows a row that is not there
    assert await claim(_Scripted(sp, reads=stale), app_version="0.6.0", content_hash="b") is False
    assert await read_stamp(sp) is None


async def test_a_lost_create_re_reads_and_finds_the_build_it_wanted_already_stored(sp):
    assert await claim(_Scripted(sp, peer=S("0.6.0", "a")), app_version="0.6.0", content_hash="a") is False
    assert (await read_stamp(sp)).content_hash == "a"


async def test_a_lost_create_re_reads_and_decides_again_for_another_build(sp):
    assert await claim(_Scripted(sp, peer=S("0.6.0", "a")), app_version="0.6.0", content_hash="b") is True
    stamp = await read_stamp(sp)
    assert (stamp.content_hash, stamp.superseded) == ("b", ["a"])


async def test_a_lost_create_to_a_newer_build_is_left_alone(sp):
    assert await claim(_Scripted(sp, peer=S("0.7.0", "z")), app_version="0.6.0", content_hash="b") is False
    assert (await read_stamp(sp)).app_version == "0.7.0"


# ---- still_mine ------------------------------------------------------------------------------


async def test_still_mine(sp):
    assert not await still_mine(sp, "a")                      # no stamp: nobody holds the wiki
    assert await claim(sp, app_version="0.6.0", content_hash="a")
    assert await still_mine(sp, "a") and not await still_mine(sp, "b")
    assert await claim(sp, app_version="0.6.0", content_hash="b")
    assert await still_mine(sp, "b") and not await still_mine(sp, "a")
