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
import socket
import threading
import time
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

    def fetch(url, *, timeout_s):
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
    def broken(url, *, timeout_s):
        raise ConnectionError("no route to host")

    problems = bake_mod.bake(tmp_path, [GOOD], fetcher=broken)
    assert len(problems) == 1 and "ConnectionError" in problems[0]
    assert list(tmp_path.iterdir()) == []


def test_each_download_gets_only_the_time_left_of_one_shared_deadline(tmp_path):
    seen: list[float] = []
    now = {"t": 100.0}

    def fetch(url, *, timeout_s):
        seen.append(timeout_s)
        now["t"] += 12.0  # this download took 12 s of the shared budget
        return GOOD.payload if url == GOOD.url else OTHER.payload

    bake_mod.bake(tmp_path, [GOOD, OTHER], fetcher=fetch, clock=lambda: now["t"], deadline_s=30.0)
    assert seen == [30.0, 18.0], "one budget for the whole bake, shrinking as it is spent"


def test_a_bake_whose_budget_is_spent_does_not_start_another_download(tmp_path):
    now = {"t": 0.0}
    calls = []

    def fetch(url, *, timeout_s):
        calls.append(timeout_s)
        now["t"] += 50.0
        if timeout_s <= 0:
            raise TimeoutError("budget spent")
        return GOOD.payload if url == GOOD.url else OTHER.payload

    problems = bake_mod.bake(tmp_path, [GOOD, OTHER], fetcher=fetch, clock=lambda: now["t"], deadline_s=30.0)
    assert calls[1] <= 0, "the second download is handed a spent budget, not a fresh one"
    assert len(problems) == 1 and "TimeoutError" in problems[0]


# ---- the hard wall-clock bound, against REAL sockets ---------------------------


def _serve(handler_body):
    """A loopback HTTP server running ``handler_body(conn)`` for one connection."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    done = threading.Event()

    def run():
        server.settimeout(5)
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            handler_body(conn)
        except OSError:
            pass
        finally:
            conn.close()
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{port}/vocab", server, done


def test_fetch_returns_what_a_server_sends():
    payload = b"v" * 300_000

    def body(conn):
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % len(payload) + payload)

    url, server, _ = _serve(body)
    try:
        assert bake_mod.fetch(url, timeout_s=10) == payload
    finally:
        server.close()


def test_a_server_trickling_one_byte_at_a_time_cannot_outlast_the_deadline():
    """Each recv lands well inside the per-operation timeout, so only the wall-clock
    bound stops it. The previous check between reads never ran: one buffered
    read blocks until its whole chunk arrives."""
    def body(conn):
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 30\r\n\r\n")
        for _ in range(30):
            conn.sendall(b"x")
            time.sleep(0.2)

    url, server, _ = _serve(body)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="did not finish within"):
            bake_mod.fetch(url, timeout_s=1.0)
    finally:
        server.close()
    assert time.monotonic() - started < 2.5, "it must give up at the deadline, not when the trickle ends"


def test_a_server_that_accepts_and_never_answers_is_bounded():
    url, server, done = _serve(lambda conn: time.sleep(5))
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            bake_mod.fetch(url, timeout_s=0.5)
    finally:
        server.close()
    assert time.monotonic() - started < 2.0


def test_anything_that_hangs_before_the_socket_such_as_dns_is_bounded_too(monkeypatch):
    """The per-operation socket timeout does not cover name resolution; the thread
    bound covers whatever urlopen is doing."""
    release = threading.Event()
    monkeypatch.setattr(bake_mod.urllib.request, "urlopen", lambda *a, **k: release.wait(5))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        bake_mod.fetch("https://example.invalid/x", timeout_s=0.3)
    release.set()
    assert time.monotonic() - started < 1.5


def test_a_download_error_reaches_the_caller_unchanged():
    url, server, _ = _serve(lambda conn: conn.sendall(b"HTTP/1.0 404 Not Found\r\n\r\n"))
    try:
        with pytest.raises(Exception) as caught:
            bake_mod.fetch(url, timeout_s=5)
    finally:
        server.close()
    assert "404" in str(caught.value)


def test_http_is_accepted_only_for_loopback():
    for host in ("127.0.0.1", "localhost"):
        url = f"http://{host}:9/x"
        try:
            bake_mod.fetch(url, timeout_s=1)
        except ValueError:
            pytest.fail(f"loopback {host} must pass the scheme check")
        except Exception:  # noqa: BLE001 - nothing listens on :9; only the scheme check matters
            pass


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


def test_the_vocab_layers_sit_below_both_uv_sync_layers():
    """A pin or script change must not invalidate the dependency install (the slow
    layer), so the COPY --from, ENV and --check come AFTER the last `uv sync`."""
    final = _dockerfile().split("AS base", 1)[1]
    last_sync = final.rindex("RUN uv sync")
    for needle in (
        "COPY --from=tokenizer-vocab /opt/primer/tiktoken-cache",
        "ENV TIKTOKEN_CACHE_DIR=/opt/primer/tiktoken-cache",
        "bake_tokenizers.py --check",
    ):
        assert final.index(needle) > last_sync, f"{needle!r} is above a uv sync layer"


def test_the_cache_dir_is_the_same_in_both_stages():
    dirs = set(re.findall(r"TIKTOKEN_CACHE_DIR=(\S+)", _dockerfile()))
    assert dirs == {"/opt/primer/tiktoken-cache"}
