#!/usr/bin/env python3
"""Bake the tokenizer vocabularies primer counts tokens with into a cache dir.

Stdlib only: the Dockerfile runs this in a tiny stage of its own, before (and
independent of) the dependency sync.

    TIKTOKEN_CACHE_DIR=/opt/primer/tiktoken-cache python3 scripts/bake_tokenizers.py
    TIKTOKEN_CACHE_DIR=/opt/primer/tiktoken-cache python3 scripts/bake_tokenizers.py --check

The first form downloads whatever is missing or corrupt, verifies every file's
sha256 against ``primer/llm/_tokenizer/vocab_pins.py`` and writes it atomically.
The second only verifies what is on disk and NEVER touches the network; it is
the build-time assertion in the image, and an operator can run it inside a
running container as a readiness check. Both exit non-zero on any failure.

Files use tiktoken's own cache layout (``sha1(url)`` as the file name), so a
stock ``tiktoken.get_encoding`` also works offline against the same directory.
The cache directory is never guessed: pass ``--dir`` or set ``TIKTOKEN_CACHE_DIR``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
_PINS_CANDIDATES = (
    HERE / "vocab_pins.py",  # flat layout inside the image build stage
    HERE.parent / "primer" / "llm" / "_tokenizer" / "vocab_pins.py",  # repo layout
)

# One socket operation may take this long; the TOTAL deadline below bounds the
# whole download, because a per-operation timeout alone does not stop a slow
# trickle from holding the build forever.
OPERATION_TIMEOUT_S = 10.0
DEFAULT_DEADLINE_S = 120.0
_CHUNK = 64 * 1024


def load_pins():
    for candidate in _PINS_CANDIDATES:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("_primer_vocab_pins", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.PINS
    raise FileNotFoundError(
        "vocab_pins.py not found next to the script or under primer/llm/_tokenizer/"
    )


def cache_path(cache_dir: str | os.PathLike, url: str) -> Path:
    """tiktoken's cache layout: the file is named by the sha1 of its URL."""
    return Path(cache_dir) / hashlib.sha1(url.encode()).hexdigest()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_verified(path: Path, sha256: str) -> bool:
    return path.is_file() and sha256_of(path) == sha256


def fetch(url: str, *, deadline: float, clock: Callable[[], float] = time.monotonic) -> bytes:
    """Download ``url`` over HTTPS, abandoning it once ``deadline`` has passed."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing a non-https vocabulary url: {url}")
    chunks: list[bytes] = []
    with urllib.request.urlopen(url, timeout=OPERATION_TIMEOUT_S) as response:  # noqa: S310
        while True:
            if clock() > deadline:
                raise TimeoutError(f"download of {url} passed its total deadline")
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def check(cache_dir: str | os.PathLike, pins: Sequence) -> list[str]:
    """Problems with what is on disk (empty list = every pinned file verifies)."""
    problems = []
    for pin in pins:
        path = cache_path(cache_dir, pin.url)
        if not path.is_file():
            problems.append(f"{pin.encoding}: missing ({path})")
        elif sha256_of(path) != pin.sha256:
            problems.append(f"{pin.encoding}: sha256 mismatch ({path})")
    return problems


def bake(
    cache_dir: str | os.PathLike,
    pins: Sequence,
    *,
    fetcher: Callable[..., bytes] = fetch,
    clock: Callable[[], float] = time.monotonic,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> list[str]:
    """Make every pinned file present and verified. Returns the problems left."""
    deadline = clock() + deadline_s
    problems = []
    for pin in pins:
        path = cache_path(cache_dir, pin.url)
        if is_verified(path, pin.sha256):
            print(f"ok {pin.encoding} sha256={pin.sha256[:12]} (already present)")
            continue
        try:
            data = fetcher(pin.url, deadline=deadline, clock=clock)
        except Exception as exc:  # noqa: BLE001 - reported, then non-zero exit
            problems.append(f"{pin.encoding}: download failed ({type(exc).__name__}: {exc})")
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != pin.sha256:
            problems.append(
                f"{pin.encoding}: downloaded bytes have sha256 {actual[:12]}, "
                f"expected {pin.sha256[:12]}; not written"
            )
            continue
        _write_atomically(path, data)
        print(f"ok {pin.encoding} sha256={pin.sha256[:12]} (downloaded)")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", help="cache directory (default: $TIKTOKEN_CACHE_DIR)")
    parser.add_argument(
        "--check", action="store_true",
        help="verify what is on disk only; never touches the network",
    )
    parser.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE_S,
                        help="total seconds allowed for all downloads")
    args = parser.parse_args(argv)

    cache_dir = args.dir or os.environ.get("TIKTOKEN_CACHE_DIR")
    if not cache_dir:
        print("refusing to guess a cache directory: pass --dir or set "
              "TIKTOKEN_CACHE_DIR", file=sys.stderr)
        return 2

    pins = load_pins()
    if args.check:
        problems = check(cache_dir, pins)
        if not problems:
            for pin in pins:
                print(f"ok {pin.encoding} sha256={pin.sha256[:12]} (verified)")
    else:
        problems = bake(cache_dir, pins, deadline_s=args.deadline)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
