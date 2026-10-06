"""``leaf_key_for``: the ``resume_event_payloads`` dict key of a dispatch key (plan 3.4 g-0 "The leaf key is encoded", d16).

``patch_if`` refuses a ``set_paths`` element that holds a double quote, a backslash or a control character
(``PatchSpecError``), because the SQLite JSON path is built as ``$."<element>"``. The leaf's dict key is the dispatch
key, whose tail is a graph NODE id (constrained only by ``min_length=1``; a fan-out instance id already carries ``[``
and ``]``) or, on a non-graph park, a raw provider ``tool_call_id``. A hook that wrote the raw key would turn a park
whose node id contains a quote or a backslash into an unwakeable park, a regression against today's whole-document
write, which accepts any key. ``leaf_key_for`` percent-encodes exactly ``%``, ``"``, ``\\`` and every character below
U+0020, leaves every other key unchanged, and is injective (``%`` is escaped too), so only uniqueness of the dict key
is at stake: the entry's own ``event_key`` stays the original string and nothing reads the dict key.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.session.yields import leaf_key_for
from primer.storage._patch import PatchSpecError, validate_patch
from primer.storage.sqlite import SqliteStorageProvider
from tests.storage import _patch_scenarios as ps

ESCAPED = ('%', '"', "\\")


def _decode(text: str) -> str:
    """The inverse of ``leaf_key_for``: every ``%XX`` is the character with that code."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "%":
            out.append(chr(int(text[i + 1 : i + 3], 16)))
            i += 3
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _patch_with_leaf(leaf: str) -> Any:
    return validate_patch(None, {("state", "resume_event_payloads", leaf): {"event_key": "k"}}, {"status": ["x"]})


@pytest.mark.parametrize(
    "key",
    [
        "a", "n1", "worker[0]", "worker[0][1]", "a:b", "a.b", "a b", "a/b", "$", "{x}", "[0]", "ünï", "\U0001f600",
        "call_9f2", "ask_user", "x" * 300, "\x7f", " ",
    ],
)
def test_a_key_without_the_four_character_classes_is_returned_unchanged(key: str) -> None:
    assert leaf_key_for(key) == key


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ('a"b', "a%22b"),
        ("a\\b", "a%5Cb"),
        ("a%b", "a%25b"),
        ("a\nb", "a%0Ab"),
        ("a\tb", "a%09b"),
        ("a\x00b", "a%00b"),
        ("a\x1fb", "a%1Fb"),
        ('n"1', "n%221"),
        ("n\\2", "n%5C2"),
        ('"', "%22"),
        ("\\\\", "%5C%5C"),
        ('x"y\\z%w', "x%22y%5Cz%25w"),
    ],
)
def test_the_four_classes_are_percent_encoded(key: str, expected: str) -> None:
    assert leaf_key_for(key) == expected


def test_the_percent_is_escaped_too_so_a_key_that_looks_encoded_stays_distinct() -> None:
    """Mutation N77: ``leaf_key_for`` does not escape ``%``. ``a"b`` becomes ``a%22b`` and the literal ``a%22b``
    becomes ``a%2522b``: two gates whose keys are these two strings keep two leaves."""
    assert leaf_key_for('a"b') == "a%22b"
    assert leaf_key_for("a%22b") == "a%2522b"
    assert leaf_key_for('a"b') != leaf_key_for("a%22b")


def test_the_mapping_is_injective_over_every_short_string_of_the_tricky_alphabet() -> None:
    alphabet = ["a", "2", "5", "C", "%", '"', "\\", "\n", "\x1f"]
    corpus = ["".join(p) for n in range(0, 5) for p in itertools.product(alphabet, repeat=n)]
    assert len(corpus) == len(set(corpus))
    encoded = {leaf_key_for(k) for k in corpus}
    assert len(encoded) == len(corpus), "two different keys were given one leaf key"


def test_the_encoding_decodes_back_to_the_key_for_a_generated_corpus() -> None:
    rng = random.Random(20251006)
    pool = [*ESCAPED, "\n", "\r", "\x00", "\x1f", "a", "b", "Z", "0", "9", ":", ".", "[", "]", " ", "é", "\U0001f600"]
    for _ in range(3000):
        key = "".join(rng.choice(pool) for _ in range(rng.randint(0, 12)))
        encoded = leaf_key_for(key)
        assert _decode(encoded) == key
        assert not any(ch in encoded for ch in ('"', "\\")) and all(ord(ch) >= 0x20 for ch in encoded)


@pytest.mark.parametrize("code", range(1, 0x80))
def test_the_output_passes_patch_ifs_key_check_for_every_ascii_character(code: int) -> None:
    """``validate_patch`` is the one place that decides which ``set_paths`` elements a backend may take."""
    key = f"node{chr(code)}x"
    _patch_with_leaf(leaf_key_for(key))


@pytest.mark.parametrize("raw", ['n"1', "n\\2", "a\nb", "a\x1fb"])
def test_the_raw_key_is_what_patch_if_refuses(raw: str) -> None:
    """The motivation, pinned: without the encoding these keys are a ``PatchSpecError``."""
    with pytest.raises(PatchSpecError):
        _patch_with_leaf(raw)
    _patch_with_leaf(leaf_key_for(raw))


@pytest_asyncio.fixture
async def sqlite_provider(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_leaves_with_quote_backslash_control_and_percent_keys_are_written_and_stay_distinct_on_real_sqlite(
    sqlite_provider: SqliteStorageProvider,
) -> None:
    """The leaves a hook would write for node ids ``n"1``, ``n\\2``, ``a"b``, the literal ``a%22b`` and a key with a
    newline, through ``patch_if`` on a real SQLite row: every write lands, the five leaves are five entries, and each
    entry keeps its ORIGINAL event key (nothing reads the dict key)."""
    store = sqlite_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="d", status="parked", state={"resume_event_payloads": {}}))
    keys = ['n"1', "n\\2", 'a"b', "a%22b", "a\nb"]
    for dispatch_key in keys:
        written = await store.patch_if(
            "d",
            None,
            set_paths={
                ("state", "resume_event_payloads", leaf_key_for(dispatch_key)): {
                    "event_key": f"ask_user:s1:{dispatch_key}",
                    "payload": {"answer": dispatch_key},
                },
            },
            where={"status": ["parked"]},
        )
        assert written is not None, f"patch_if refused the leaf of {dispatch_key!r}"

    row = await store.get("d")
    leaves = row.state["resume_event_payloads"]
    assert sorted(e["event_key"] for e in leaves.values()) == sorted(f"ask_user:s1:{k}" for k in keys)
    assert {e["payload"]["answer"] for e in leaves.values()} == set(keys)
    assert len(leaves) == len(keys), "two dispatch keys shared one leaf"
    assert set(leaves) == {leaf_key_for(k) for k in keys}


@pytest.mark.asyncio
async def test_a_raw_quote_key_is_refused_by_patch_if_on_real_sqlite(sqlite_provider: SqliteStorageProvider) -> None:
    """Why the helper exists, on the real backend: the raw key never reaches SQLite, it is a PatchSpecError first."""
    store = sqlite_provider.get_storage(ps.PatchDoc)
    await store.create(ps.PatchDoc(id="d", status="parked", state={"resume_event_payloads": {}}))
    with pytest.raises(PatchSpecError):
        await store.patch_if(
            "d", None, set_paths={("state", "resume_event_payloads", 'n"1'): {"x": 1}}, where={"status": ["parked"]},
        )
