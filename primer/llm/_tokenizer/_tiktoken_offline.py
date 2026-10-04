"""Offline loader for the tiktoken vocabularies primer counts tokens with.

``tiktoken.get_encoding`` is unsafe on a production turn path: when its cached
vocabulary file is missing, or fails its hash check (it deletes the file
first), it re-fetches over the network with ``requests.get`` and no timeout,
whatever ``TIKTOKEN_CACHE_DIR`` says. This module never calls it. It

1. reads the vocabulary file from the cache directory itself,
2. verifies its sha256 against the pins in :mod:`vocab_pins` (and leaves a bad
   file where it is: a counter must not delete operator state),
3. builds the :class:`tiktoken.Encoding` from the verified bytes, and
4. raises :class:`~primer.model.except_.TokenCounterUnavailable` when anything
   is missing, so the one wrapper that owns fallback (``primer.llm.counting``)
   labels the estimate instead of a counter quietly returning a heuristic.

The cache directory is resolved exactly as tiktoken resolves it
(``TIKTOKEN_CACHE_DIR``, then ``DATA_GYM_CACHE_DIR``, then
``<tmp>/data-gym-cache``), read-only, so the directory the image bakes
(``scripts/bake_tokenizers.py``) and a developer's warm tiktoken cache both
work. An empty value, tiktoken's "disable caching", means no directory.

Successes are memoised for the life of the process, and so are the failures that
cannot cure themselves (a vocabulary file that is absent, fails its hash check or
will not build, or no cache directory at all): those are not retried on every
turn. A transient OS error reading the file (EMFILE, EACCES, EIO) is NOT
memoised: it is raised, logged, and the next attempt reads the file again, so one
bad moment cannot disable native counting until restart.

The encoding's pattern and special tokens come from tiktoken's own
constructor in ``tiktoken_ext.openai_public``, run against a copy of its
globals in which ``load_tiktoken_bpe`` returns our verified ranks. Nothing is
duplicated here that a tiktoken release could change underneath us, and no
module global is patched, so a concurrent ``tiktoken.get_encoding`` is
unaffected.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
import threading
import types
from collections.abc import Callable, Sequence
from pathlib import Path

import tiktoken

import primer.observability.metrics as _metrics
from primer.llm._tokenizer.vocab_pins import PINS, VocabPin
from primer.model.except_ import TokenCounterUnavailable


logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ENCODINGS: dict[tuple[str, str, str], tiktoken.Encoding] = {}
# Process-lifetime negative cache: same key -> why it is unavailable.
_UNAVAILABLE: dict[tuple[str, str, str], str] = {}


def resolve_cache_dir() -> Path | None:
    """The vocabulary directory, resolved the way tiktoken resolves it."""
    for variable in ("TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR"):
        if variable in os.environ:
            value = os.environ[variable]
            return Path(value) if value else None
    return Path(tempfile.gettempdir()) / "data-gym-cache"


def vocab_path(cache_dir: Path, pin: VocabPin) -> Path:
    """tiktoken's cache layout: the file is named by the sha1 of its URL."""
    return cache_dir / hashlib.sha1(pin.url.encode()).hexdigest()


def parse_ranks(data: bytes) -> dict[bytes, int]:
    """The ``.tiktoken`` format: one ``<base64 token> <rank>`` pair per line."""
    ranks: dict[bytes, int] = {}
    for line in data.splitlines():
        if not line:
            continue
        token, rank = line.split()
        ranks[base64.b64decode(token)] = int(rank)
    return ranks


def build_encoding(
    constructor: Callable[[], dict], ranks: dict[bytes, int],
) -> tiktoken.Encoding:
    """Run tiktoken's own encoding constructor against our verified ranks.

    ``constructor`` calls ``load_tiktoken_bpe(url, expected_hash=...)`` and
    returns the ``Encoding`` keyword arguments. The copy of the function sees a
    ``load_tiktoken_bpe`` that returns ``ranks`` and touches nothing else.
    """
    isolated = types.FunctionType(
        constructor.__code__,
        {**constructor.__globals__, "load_tiktoken_bpe": lambda *_a, **_k: ranks},
        constructor.__name__,
        constructor.__defaults__,
        constructor.__closure__,
    )
    return tiktoken.Encoding(**isolated())


def _default_constructor(name: str) -> Callable[[], dict]:
    import tiktoken_ext.openai_public as public

    try:
        return public.ENCODING_CONSTRUCTORS[name]
    except KeyError:
        raise TokenCounterUnavailable(
            f"the installed tiktoken has no constructor for {name!r}"
        ) from None


def load_encoding(
    name: str,
    *,
    pins: Sequence[VocabPin] = PINS,
    cache_dir: Path | None = None,
    constructor: Callable[[], dict] | None = None,
) -> tiktoken.Encoding:
    """The verified encoding ``name``, or :class:`TokenCounterUnavailable`.

    Never touches the network and never raises anything else. ``pins``,
    ``cache_dir`` and ``constructor`` exist so tests can supply their own;
    production callers pass only ``name``.
    """
    resolved_dir = cache_dir if cache_dir is not None else resolve_cache_dir()
    pin = next((p for p in pins if p.encoding == name), None)
    if pin is None:
        raise TokenCounterUnavailable(f"no pinned vocabulary for encoding {name!r}")
    key = (name, str(resolved_dir), pin.sha256)

    with _LOCK:
        if key in _ENCODINGS:
            _metrics.llm_tokenizer_ready.labels(name).set(1)
            return _ENCODINGS[key]
        if key in _UNAVAILABLE:
            _metrics.llm_tokenizer_ready.labels(name).set(0)
            raise TokenCounterUnavailable(_UNAVAILABLE[key])
        try:
            encoding = _load(pin, resolved_dir, constructor)
        except TokenCounterUnavailable as exc:
            _metrics.llm_tokenizer_ready.labels(name).set(0)
            if exc.transient:
                # A momentary OS condition (EMFILE, EACCES, EIO): not remembered,
                # so one bad moment cannot disable counting for the process.
                logger.warning(
                    "tokenizer vocabulary %s could not be read just now: %s",
                    name, exc.message,
                )
                raise
            _UNAVAILABLE[key] = exc.message
            logger.warning(
                "tokenizer vocabulary %s unavailable, native counts disabled "
                "for it until restart: %s", name, exc.message,
            )
            raise
        _ENCODINGS[key] = encoding
        _metrics.llm_tokenizer_ready.labels(name).set(1)
        return encoding


def _load(
    pin: VocabPin,
    cache_dir: Path | None,
    constructor: Callable[[], dict] | None,
) -> tiktoken.Encoding:
    if cache_dir is None:
        raise TokenCounterUnavailable(
            f"{pin.encoding}: no tokenizer cache directory (TIKTOKEN_CACHE_DIR is empty)"
        )
    path = vocab_path(cache_dir, pin)
    try:
        data = path.read_bytes()
    except OSError as exc:
        # Absent is permanent-until-restart; any other OS error is a moment.
        absent = isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError))
        raise TokenCounterUnavailable(
            f"{pin.encoding}: vocabulary file not readable at {path} ({exc.strerror or exc})",
            transient=not absent,
            cause=exc,
        ) from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != pin.sha256:
        raise TokenCounterUnavailable(
            f"{pin.encoding}: {path} has sha256 {actual[:12]}, expected "
            f"{pin.sha256[:12]}; refusing to use it (the file is left in place)"
        )
    try:
        ranks = parse_ranks(data)
        return build_encoding(constructor or _default_constructor(pin.encoding), ranks)
    except TokenCounterUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - a malformed file or a changed tiktoken
        raise TokenCounterUnavailable(
            f"{pin.encoding}: could not build the encoding ({type(exc).__name__}: {exc})",
            cause=exc,
        ) from exc


def reset() -> None:
    """Forget every memoised encoding and failure (tests, and a re-bake)."""
    with _LOCK:
        _ENCODINGS.clear()
        _UNAVAILABLE.clear()


__all__ = [
    "build_encoding",
    "load_encoding",
    "parse_ranks",
    "reset",
    "resolve_cache_dir",
    "vocab_path",
]
