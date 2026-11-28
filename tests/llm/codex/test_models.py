"""Bounded catalogue compatibility hints are not account authorization."""

from __future__ import annotations

import json
import os

import pytest

from primer.llm.codex.models import (
    MAX_CATALOG_BYTES,
    SOURCE_REFERENCE,
    is_direct_model,
)


def test_source_reference_does_not_admit_gpt_5_5() -> None:
    assert SOURCE_REFERENCE == "rust-v0.169.0"
    assert is_direct_model("gpt-5.5", catalog_path=None) is False


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
    assert is_direct_model("gpt-5.5", catalog_path=str(broken)) is False


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
    """Nonblocking open followed by fstat refuses a FIFO without a writer."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert is_direct_model("anything", catalog_path=str(fifo)) is False


def test_replacement_with_fifo_at_open_is_nonblocking(tmp_path, monkeypatch) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text('[{"slug":"model","tool_mode":"direct"}]')
    real_open = os.open
    opened = []

    def replace_then_open(path, flags):
        assert flags & os.O_NONBLOCK
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_CLOEXEC
        catalog.unlink()
        os.mkfifo(catalog)
        fd = real_open(path, flags)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", replace_then_open)
    assert is_direct_model("model", catalog_path=str(catalog)) is False
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_read_uses_validated_descriptor_after_path_replacement(tmp_path, monkeypatch) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text('[{"slug":"original","tool_mode":"direct"}]')
    real_fstat = os.fstat
    checked = []

    def replace_after_fstat(fd):
        info = real_fstat(fd)
        checked.append(fd)
        catalog.unlink()
        catalog.write_text('[{"slug":"replacement","tool_mode":"direct"}]')
        return info

    monkeypatch.setattr(os, "fstat", replace_after_fstat)
    assert is_direct_model("original", catalog_path=str(catalog)) is True
    assert len(checked) == 1
    with pytest.raises(OSError):
        real_fstat(checked[0])


def test_final_symlink_refused(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text('[{"slug":"model","tool_mode":"direct"}]')
    link = tmp_path / "catalog"
    link.symlink_to(target)
    assert is_direct_model("model", catalog_path=str(link)) is False
    assert is_direct_model("model", catalog_path=str(target)) is True


@pytest.mark.parametrize("operation", ["open", "fstat", "read", "close"])
def test_io_failure_refuses_and_disposes_descriptor(tmp_path, monkeypatch, operation) -> None:
    catalog = tmp_path / "catalog"
    catalog.write_text('[{"slug":"model","tool_mode":"direct"}]')
    real_open, real_close = os.open, os.close
    opened, closed = [], []

    def tracked_open(*args):
        if operation == "open":
            raise PermissionError("private pathname must not escape")
        fd = real_open(*args)
        opened.append(fd)
        return fd

    def tracked_close(fd):
        closed.append(fd)
        real_close(fd)
        if operation == "close":
            raise OSError("uncertain close must not admit")

    def fail(*args):
        raise OSError("read failure")

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    if operation in {"fstat", "read"}:
        monkeypatch.setattr(os, operation, fail)
    assert is_direct_model("model", catalog_path=str(catalog)) is False
    assert closed == opened


def test_growth_after_fstat_is_still_bounded(tmp_path, monkeypatch) -> None:
    catalog = tmp_path / "catalog"
    catalog.write_text("[]")
    real_fstat, real_read = os.fstat, os.read
    read_sizes = []

    def grow(fd):
        info = real_fstat(fd)
        catalog.write_bytes(b" " * (MAX_CATALOG_BYTES + 2))
        return info

    def read(fd, size):
        data = real_read(fd, size)
        read_sizes.append(len(data))
        return data

    monkeypatch.setattr(os, "fstat", grow)
    monkeypatch.setattr(os, "read", read)
    assert is_direct_model("model", catalog_path=str(catalog)) is False
    assert sum(read_sizes) == MAX_CATALOG_BYTES + 1


@pytest.mark.parametrize("raw", [
    b'[{"slug":"model","tool_mode":"direct"},{"slug":"model"}]',
    b'[{"slug":"model","tool_mode":"direct"},null]',
    b'[{"slug":"model","tool_mode":"other","tool_mode":"direct"}]',
    b'{"models":[],"models":[{"slug":"model","tool_mode":"direct"}]}',
    b'[{"slug":"model","tool_mode":"direct","other":NaN}]',
    b'\xff',
])
def test_ambiguous_or_invalid_catalogue_refuses(tmp_path, raw) -> None:
    catalog = tmp_path / "catalog"
    catalog.write_bytes(raw)
    assert is_direct_model("model", catalog_path=str(catalog)) is False
