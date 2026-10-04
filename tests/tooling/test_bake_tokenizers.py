"""scripts/bake_tokenizers.py and the Dockerfile stage that runs it.

The bake exists because tiktoken's own loader refetches over the network, with
no timeout, whenever its cache misses or fails the hash check. These tests pin
the properties that make a baked image trustworthy: files are verified against
pinned sha256 values directly (not through tiktoken), a bad download is never
written, ``--check`` never touches the network, and the pins agree with the
tiktoken that is actually installed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "bake_tokenizers", ROOT / "scripts" / "bake_tokenizers.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bake_mod = _load_script()


class _Pin:
    def __init__(self, encoding: str, payload: bytes):
        self.encoding = encoding
        self.url = f"https://example.invalid/encodings/{encoding}.tiktoken"
        self.sha256 = hashlib.sha256(payload).hexdigest()
        self.payload = payload


GOOD = _Pin("good_base", b"IQ== 0\nIg== 1\n")
OTHER = _Pin("other_base", b"Iw== 0\nJA== 1\n")


def _fetcher(pins, log):
    by_url = {p.url: p.payload for p in pins}

    def fetch(url, *, deadline, clock):
        log.append(url)
        return by_url[url]

    return fetch


def test_bake_writes_each_file_under_tiktokens_cache_layout(tmp_path):
    log: list[str] = []
    problems = bake_mod.bake(tmp_path, [GOOD, OTHER], fetcher=_fetcher([GOOD, OTHER], log))
    assert problems == []
    for pin in (GOOD, OTHER):
        path = tmp_path / hashlib.sha1(pin.url.encode()).hexdigest()
        assert path.read_bytes() == pin.payload
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        hashlib.sha1(p.url.encode()).hexdigest() for p in (GOOD, OTHER)
    ), "no temp file may be left behind"


def test_a_verified_file_is_not_downloaded_again(tmp_path):
    log: list[str] = []
    bake_mod.bake(tmp_path, [GOOD], fetcher=_fetcher([GOOD], log))
    log.clear()
    assert bake_mod.bake(tmp_path, [GOOD], fetcher=_fetcher([GOOD], log)) == []
    assert log == []


def test_a_corrupt_cached_file_is_replaced(tmp_path):
    path = bake_mod.cache_path(tmp_path, GOOD.url)
    path.write_bytes(b"truncated")
    log: list[str] = []
    assert bake_mod.bake(tmp_path, [GOOD], fetcher=_fetcher([GOOD], log)) == []
    assert log == [GOOD.url]
    assert path.read_bytes() == GOOD.payload


def test_downloaded_bytes_that_fail_the_hash_are_never_written(tmp_path):
    tampered = _Pin("good_base", b"IQ== 0\n")
    tampered.url = GOOD.url  # same address, different bytes than the pin expects
    pin = _Pin("good_base", GOOD.payload)
    problems = bake_mod.bake(
        tmp_path, [pin], fetcher=_fetcher([tampered], []),
    )
    assert len(problems) == 1 and "not written" in problems[0]
    assert list(tmp_path.iterdir()) == []


def test_a_failed_download_is_reported_not_raised(tmp_path):
    def broken(url, *, deadline, clock):
        raise ConnectionError("no route to host")

    problems = bake_mod.bake(tmp_path, [GOOD], fetcher=broken)
    assert len(problems) == 1 and "ConnectionError" in problems[0]
    assert list(tmp_path.iterdir()) == []


def test_the_total_deadline_is_handed_to_every_download(tmp_path):
    seen: list[float] = []

    def fetch(url, *, deadline, clock):
        seen.append(deadline)
        return GOOD.payload if url == GOOD.url else OTHER.payload

    bake_mod.bake(tmp_path, [GOOD, OTHER], fetcher=fetch, clock=lambda: 100.0,
                  deadline_s=30.0)
    assert seen == [130.0, 130.0], "one deadline for the whole bake, not per file"


def test_fetch_gives_up_once_the_deadline_has_passed(monkeypatch):
    class _Slow:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _n):
            return b"x" * 10  # never ends: a slow trickle

    monkeypatch.setattr(bake_mod.urllib.request, "urlopen", lambda *a, **k: _Slow())
    ticks = iter([0.0, 1.0, 2.0, 99.0])
    with pytest.raises(TimeoutError, match="total deadline"):
        bake_mod.fetch(GOOD.url, deadline=10.0, clock=lambda: next(ticks))


def test_fetch_refuses_a_non_https_url():
    with pytest.raises(ValueError, match="non-https"):
        bake_mod.fetch("http://example.invalid/x", deadline=1e18)


def test_check_passes_on_verified_files_and_never_downloads(tmp_path, monkeypatch):
    for pin in (GOOD, OTHER):
        bake_mod.cache_path(tmp_path, pin.url).write_bytes(pin.payload)
    monkeypatch.setattr(
        bake_mod, "fetch",
        lambda *a, **k: pytest.fail("--check must never touch the network"),
    )
    assert bake_mod.check(tmp_path, [GOOD, OTHER]) == []


def test_check_reports_a_missing_and_a_corrupt_file(tmp_path):
    bake_mod.cache_path(tmp_path, GOOD.url).write_bytes(b"corrupt")
    problems = bake_mod.check(tmp_path, [GOOD, OTHER])
    assert any("good_base: sha256 mismatch" in p for p in problems)
    assert any("other_base: missing" in p for p in problems)


def test_main_refuses_to_guess_a_cache_directory(monkeypatch, capsys):
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    assert bake_mod.main(["--check"]) == 2
    assert "refusing to guess" in capsys.readouterr().err


def test_main_check_exits_nonzero_when_the_pinned_files_are_absent(tmp_path, capsys):
    assert bake_mod.main(["--check", "--dir", str(tmp_path)]) == 1
    assert "FAIL" in capsys.readouterr().err


# ---- the pins themselves ----------------------------------------------------


def test_pins_agree_with_the_installed_tiktoken():
    """A tiktoken bump that moves a vocabulary must fail here, not at runtime."""
    import tiktoken_ext.openai_public as public

    source = Path(public.__file__).read_text()
    for pin in bake_mod.load_pins():
        found = re.search(
            rf'"(https://[^"]*/{re.escape(pin.encoding)}\.tiktoken)",'
            r'\s*expected_hash="([0-9a-f]{64})"',
            source,
        )
        assert found, f"tiktoken no longer lists {pin.encoding}"
        assert (pin.url, pin.sha256) == found.groups()


def test_pins_module_is_stdlib_only():
    """The image bake loads it before any dependency is installed."""
    text = (ROOT / "primer" / "llm" / "_tokenizer" / "vocab_pins.py").read_text()
    imports = re.findall(r"^(?:from|import)\s+(\w+)", text, re.M)
    assert set(imports) <= {"__future__", "typing"}, imports


# ---- the Dockerfile wiring (static: the real build is exercised at release) --


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text()


def test_dockerfile_bakes_in_its_own_stage_and_asserts_at_build_time():
    text = _dockerfile()
    assert re.search(r"^FROM python:3\.12-slim AS tokenizer-vocab$", text, re.M)
    stage = text.split("AS tokenizer-vocab", 1)[1].split("\nFROM ", 1)[0]
    assert "bake_tokenizers.py" in stage
    assert "--check" in stage, "the stage must re-verify what it just wrote"


def test_dockerfile_ships_the_baked_vocab_and_verifies_it_in_the_final_stage():
    text = _dockerfile()
    final = text.split("AS base", 1)[1]
    assert "COPY --from=tokenizer-vocab /opt/primer/tiktoken-cache" in final
    assert "ENV TIKTOKEN_CACHE_DIR=/opt/primer/tiktoken-cache" in final
    assert "bake_tokenizers.py --check" in final


def test_the_cache_dir_is_the_same_in_both_stages():
    dirs = set(re.findall(r"TIKTOKEN_CACHE_DIR=(\S+)", _dockerfile()))
    assert dirs == {"/opt/primer/tiktoken-cache"}
