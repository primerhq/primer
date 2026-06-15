from primer.channel.telegram.adapter import _BoundedDict, _CACHE_MAXSIZE


def test_insert_up_to_maxsize_keeps_all_entries():
    d = _BoundedDict(maxsize=3)
    d["a"] = 1
    d["b"] = 2
    d["c"] = 3
    assert len(d) == 3
    assert list(d.keys()) == ["a", "b", "c"]
    assert d["a"] == 1 and d["b"] == 2 and d["c"] == 3


def test_overflow_evicts_oldest_fifo():
    d = _BoundedDict(maxsize=3)
    d["a"] = 1
    d["b"] = 2
    d["c"] = 3
    # The 4th insertion overflows; oldest ("a") is evicted.
    d["d"] = 4
    assert len(d) == 3
    assert list(d.keys()) == ["b", "c", "d"]
    assert "a" not in d


def test_reinsert_refreshes_recency_so_a_different_key_is_evicted():
    d = _BoundedDict(maxsize=3)
    d["a"] = 1
    d["b"] = 2
    d["c"] = 3
    # Re-insert the oldest key "a" -> moves it to most-recent.
    d["a"] = 10
    assert list(d.keys()) == ["b", "c", "a"]
    # Now overflow: the (new) oldest is "b", not the re-inserted "a".
    d["d"] = 4
    assert len(d) == 3
    assert list(d.keys()) == ["c", "a", "d"]
    assert "b" not in d
    assert d["a"] == 10


def test_reading_evicted_key_returns_none_via_get():
    d = _BoundedDict(maxsize=2)
    d["a"] = 1
    d["b"] = 2
    d["c"] = 3  # evicts "a"
    assert d.get("a") is None
    assert "a" not in d
    assert d.get("b") == 2
    assert d.get("c") == 3


def test_adapter_constructs_bounded_caches_with_cache_maxsize():
    # Lightweight construction: the adapter __init__ only stores its kwargs and
    # builds the two _BoundedDict caches, so no heavy fixtures are needed.
    from primer.channel.telegram.adapter import TelegramChannelAdapter

    adapter = TelegramChannelAdapter(
        provider=None, channel=None, inbox=None,
    )
    assert isinstance(adapter._tag_cache, _BoundedDict)
    assert isinstance(adapter._reply_targets, _BoundedDict)
    assert adapter._tag_cache._maxsize == _CACHE_MAXSIZE
    assert adapter._reply_targets._maxsize == _CACHE_MAXSIZE
