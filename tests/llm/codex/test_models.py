"""The codex provider's direct-model rule (the codex plan, task 4; spec section 6.6).

Only models that call tools directly may be used, so Codex's JavaScript
``exec`` and ``wait`` tools are never registered. A model is direct when it
is in ``CODEX_DIRECT_MODELS`` (verified at the tested Codex version) or the
operator's model catalog file (Codex's ``model_catalog_json``) marks its
entry ``"tool_mode": "direct"``. Primer reads only that operator-supplied
file, never Codex's own cache. Any read or parse error, a missing file, or
a model absent from the file means not direct (``False``), never an
exception: a broken catalog must refuse the model, not take the provider
down.
"""

from __future__ import annotations

import json
import os

from primer.llm.codex.models import (
    CODEX_DIRECT_MODELS,
    CODEX_MAX_TESTED_VERSION,
    MAX_CATALOG_BYTES,
    MINIMUM_VERSION,
    is_direct_model,
)


def test_gpt_5_5_is_direct() -> None:
    assert is_direct_model("gpt-5.5", catalog_path=None) is True
    assert CODEX_DIRECT_MODELS == frozenset({"gpt-5.5"})
    assert MINIMUM_VERSION == "0.169.0"
    assert CODEX_MAX_TESTED_VERSION == "0.169.0"


def test_catalog_direct_entry_is_direct(tmp_path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [
        {"slug": "my-frontier", "tool_mode": "direct"},
        {"slug": "other", "tool_mode": "code_mode_only"},
    ]}), encoding="utf-8")
    assert is_direct_model("my-frontier", catalog_path=str(catalog)) is True
    # a top-level list of entries is the same file, read the same way
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps([{"slug": "flat-model", "tool_mode": "direct"}]), encoding="utf-8")
    assert is_direct_model("flat-model", catalog_path=str(flat)) is True


def test_catalog_non_direct_entry_is_not(tmp_path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [
        {"slug": "other", "tool_mode": "code_mode_only"},
        {"slug": "no-mode"},
    ]}), encoding="utf-8")
    assert is_direct_model("other", catalog_path=str(catalog)) is False
    assert is_direct_model("no-mode", catalog_path=str(catalog)) is False
    assert is_direct_model("absent", catalog_path=str(catalog)) is False


def test_unreadable_catalog_is_not_direct(tmp_path) -> None:
    assert is_direct_model("anything", catalog_path=str(tmp_path / "nope.json")) is False
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert is_direct_model("anything", catalog_path=str(broken)) is False
    # the verified set is consulted before the catalog, so gpt-5.5 stays
    # direct even when the file cannot be read
    assert is_direct_model("gpt-5.5", catalog_path=str(broken)) is True


def test_deeply_nested_catalog_is_not_direct(tmp_path) -> None:
    """json.loads raises RecursionError (a RuntimeError) on this file; the
    reader must treat it as not direct, not propagate it."""
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 200_000, encoding="utf-8")
    assert is_direct_model("anything", catalog_path=str(deep)) is False


def test_over_cap_catalog_is_not_direct(tmp_path) -> None:
    big = tmp_path / "big.json"
    big.write_bytes(b"[" * (MAX_CATALOG_BYTES + 2))
    assert is_direct_model("anything", catalog_path=str(big)) is False


def test_directory_is_not_direct(tmp_path) -> None:
    assert is_direct_model("anything", catalog_path=str(tmp_path)) is False


def test_fifo_is_not_direct(tmp_path) -> None:
    """A FIFO at the configured path must be refused by the stat gate, not
    opened (opening it would block the reader, and with it the event
    loop, on the write path)."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert is_direct_model("anything", catalog_path=str(fifo)) is False
