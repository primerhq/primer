"""primer.llm._tokenizer._tiktoken_offline: the loader that never fetches.

tiktoken's own loader re-fetches over the network, with no timeout, whenever a
cached vocabulary file is missing or fails its hash check. These tests pin the
properties that replace it: files are verified against pinned sha256 values, a
bad file is left in place, a missing one raises a typed error without ever
reaching ``requests``, and results (failures included) are memoised.

They opt out of the autouse ``offline_tiktoken`` fixture (which replaces the
loader with an in-memory encoding) and build everything in ``tmp_path``.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
import tiktoken

from primer.llm._tokenizer import _tiktoken_offline as offline
from primer.llm._tokenizer.vocab_pins import VocabPin
from primer.model.except_ import TokenCounterUnavailable

pytestmark = pytest.mark.real_tiktoken_loader

URL = "https://example.invalid/encodings/fake_base.tiktoken"


def load_tiktoken_bpe(*_args, **_kwargs):
    """Stands in for tiktoken's loader in this module's globals. The isolated
    constructor copy must NOT see this one."""
    raise AssertionError("the real load_tiktoken_bpe must never run")


def fake_constructor() -> dict:
    ranks = load_tiktoken_bpe(URL, expected_hash="ignored")
    return {
        "name": "fake_base",
        "pat_str": r"(?s:.)",
        "mergeable_ranks": ranks,
        "special_tokens": {"<|endoftext|>": len(ranks)},
    }


def _vocab_bytes() -> bytes:
    return b"\n".join(
        base64.b64encode(bytes([i])) + b" " + str(i).encode() for i in range(256)
    ) + b"\n"


@pytest.fixture(autouse=True)
def _clean():
    offline.reset()
    yield
    offline.reset()


@pytest.fixture
def no_network(monkeypatch):
    import requests

    def boom(*_a, **_k):
        pytest.fail("the offline loader reached the network")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests.Session, "request", boom)


@pytest.fixture
def vocab(tmp_path: Path):
    data = _vocab_bytes()
    pin = VocabPin("fake_base", URL, hashlib.sha256(data).hexdigest())
    path = offline.vocab_path(tmp_path, pin)
    path.write_bytes(data)
    return pin, path, tmp_path


def _load(pin: VocabPin, cache_dir: Path, **kwargs):
    return offline.load_encoding(
        "fake_base", pins=(pin,), cache_dir=cache_dir,
        constructor=kwargs.pop("constructor", fake_constructor), **kwargs,
    )


def test_a_verified_file_loads_and_counts(vocab, no_network):
    pin, _path, cache_dir = vocab
    encoding = _load(pin, cache_dir)
    assert encoding.encode_ordinary("héllo") == list("héllo".encode())


def test_the_constructor_sees_our_ranks_without_patching_any_global(vocab):
    pin, _path, cache_dir = vocab
    _load(pin, cache_dir)  # fake_constructor calls load_tiktoken_bpe: ours, not this module's
    with pytest.raises(AssertionError, match="must never run"):
        load_tiktoken_bpe()  # the module global is untouched


def test_the_encoding_registers_special_tokens_so_encode_would_raise(vocab):
    """Why the counter must use encode_ordinary: content that spells a
    special token (a web page, a file a tool read) makes ``encode`` raise."""
    pin, _path, cache_dir = vocab
    encoding = _load(pin, cache_dir)
    with pytest.raises(ValueError, match="disallowed special token"):
        encoding.encode("tool output: <|endoftext|> end")
    assert encoding.encode_ordinary("tool output: <|endoftext|> end")


def test_a_loaded_encoding_is_memoised(vocab):
    pin, path, cache_dir = vocab
    first = _load(pin, cache_dir)
    path.unlink()
    assert _load(pin, cache_dir) is first


def test_a_missing_file_is_unavailable_and_never_reaches_the_network(tmp_path, no_network):
    pin = VocabPin("fake_base", URL, "0" * 64)
    with pytest.raises(TokenCounterUnavailable, match="not readable") as exc:
        _load(pin, tmp_path)
    assert exc.value.transient is False


def test_a_failure_is_remembered_for_the_life_of_the_process(vocab):
    pin, path, cache_dir = vocab
    path.write_bytes(b"corrupt")
    with pytest.raises(TokenCounterUnavailable):
        _load(pin, cache_dir)
    path.write_bytes(_vocab_bytes())  # healed on disk
    with pytest.raises(TokenCounterUnavailable):
        _load(pin, cache_dir)  # still unavailable: no retry per call
    offline.reset()
    assert _load(pin, cache_dir)  # a re-bake plus reset recovers


def test_a_hash_mismatch_is_refused_and_the_file_is_left_in_place(vocab, no_network):
    """tiktoken would delete it and fetch; a counter must not touch operator state."""
    pin, path, cache_dir = vocab
    path.write_bytes(b"tampered")
    with pytest.raises(TokenCounterUnavailable, match="refusing to use it"):
        _load(pin, cache_dir)
    assert path.read_bytes() == b"tampered"


def test_unparsable_bytes_with_a_valid_hash_are_unavailable(tmp_path):
    data = b"not a vocabulary\n"
    pin = VocabPin("fake_base", URL, hashlib.sha256(data).hexdigest())
    offline.vocab_path(tmp_path, pin).write_bytes(data)
    with pytest.raises(TokenCounterUnavailable, match="could not build"):
        _load(pin, tmp_path)


def test_a_constructor_that_fails_is_unavailable(vocab):
    pin, _path, cache_dir = vocab

    def broken() -> dict:
        raise RuntimeError("tiktoken changed")

    with pytest.raises(TokenCounterUnavailable, match="could not build"):
        _load(pin, cache_dir, constructor=broken)


def test_an_unpinned_encoding_is_unavailable(tmp_path):
    with pytest.raises(TokenCounterUnavailable, match="no pinned vocabulary"):
        offline.load_encoding("gpt2", cache_dir=tmp_path)


def test_a_disabled_cache_dir_is_unavailable(monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
    assert offline.resolve_cache_dir() is None
    with pytest.raises(TokenCounterUnavailable, match="no tokenizer cache directory"):
        offline.load_encoding("o200k_base")


def test_cache_dir_resolution_follows_tiktoken(monkeypatch, tmp_path):
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.delenv("DATA_GYM_CACHE_DIR", raising=False)
    assert offline.resolve_cache_dir().name == "data-gym-cache"
    monkeypatch.setenv("DATA_GYM_CACHE_DIR", str(tmp_path / "gym"))
    assert offline.resolve_cache_dir() == tmp_path / "gym"
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "primary"))
    assert offline.resolve_cache_dir() == tmp_path / "primary"


def test_parse_ranks_matches_tiktokens_own_parser(tmp_path, monkeypatch):
    from tiktoken.load import load_tiktoken_bpe as stock

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")  # tiktoken: caching disabled
    path = tmp_path / "v.tiktoken"
    path.write_bytes(_vocab_bytes())
    assert offline.parse_ranks(path.read_bytes()) == stock(str(path))


def _real_vocab_available() -> bool:
    cache_dir = offline.resolve_cache_dir()
    if cache_dir is None:
        return False
    from primer.llm._tokenizer.vocab_pins import PINS

    for pin in PINS:
        path = offline.vocab_path(cache_dir, pin)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != pin.sha256:
            return False
    return True


@pytest.mark.skipif(
    not _real_vocab_available(),
    reason="the real vocabularies are not in the cache dir (bake them: scripts/bake_tokenizers.py)",
)
@pytest.mark.parametrize("name", ["o200k_base", "cl100k_base"])
def test_real_vocabulary_matches_stock_tiktoken(name):
    """Needs the baked vocabularies (the image has them, a dev box with a warm
    tiktoken cache has them); proves the isolated-constructor build is exact."""
    ours = offline.load_encoding(name)
    stock = tiktoken.get_encoding(name)
    assert ours.n_vocab == stock.n_vocab
    assert ours.special_tokens_set == stock.special_tokens_set
    for sample in ("hello world", "日本語のテキスト", "😀 emoji 🚀", "a <|endoftext|> b", '{"k": [1, 2]}'):
        assert ours.encode_ordinary(sample) == stock.encode_ordinary(sample)
