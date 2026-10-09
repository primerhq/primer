"""What a failed model call says: the provider by its configured id and kind, and the provider's own words (C-024).

The shared classifiers (``primer.common.openai_errors`` and its siblings, ``_classify_ollama_exception``) choose the exception CLASS, the ``code``
(``server_error``, ``rate_limit``, ``auth_error``, ``network_error``: what the console's failure words read) and the ``status_code``. Their messages
were fixed labels naming a vendor ("OpenAI server error"), which dropped what the provider said and called every OpenAI-compatible endpoint (LM
Studio, vLLM, a proxy) OpenAI. Each adapter now passes the classified error through :func:`describe_failure`, which keeps all of that and rewrites
the MESSAGE of the four shapes that lost the provider's words:

    Model provider 'lm-studio-box' (openchat/lmstudio) had a server error (HTTP 500): upstream exploded

The provider text is the response body's ``error.message`` (else a bounded slice of the body), whitespace collapsed, with credentials masked and
the result capped at :data:`UPSTREAM_TEXT_CAP` characters. A request the provider REJECTED (4xx) keeps the message the classifier gave it:
``primer.common.context_overflow`` reads that message, and the SDK's text already carries the provider's words. It is only scrubbed.

Masking is done BEFORE the cap, so a secret is never cut in half and left partly readable. What is masked: the provider's configured API key and
Base URL password (exact values: the strongest rule, and the one that catches a key echoed back in a body), the ``Authorization: Basic`` token a Base
URL with credentials is sent as, URL-borne credentials (``primer.common.log.redact_url_secrets``) and ``Bearer`` / ``Basic`` tokens. Control characters are
stripped first, so a NUL inside an echoed key does not defeat the exact match.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any
from urllib.parse import unquote

from primer.common.log import redact_url_secrets
from primer.model.except_ import (
    AuthenticationError,
    NetworkError,
    PrimerError,
    RateLimitError,
    ServerError,
)

logger = logging.getLogger(__name__)

__all__ = ["UPSTREAM_TEXT_CAP", "describe_failure", "provider_label", "scrub"]

#: Longest the provider's own text may be in a failure message (the console shows the message as the card's detail, and a 502 from a proxy can be
#: a whole HTML page).
UPSTREAM_TEXT_CAP = 300

#: Longest an UNTOUCHED message (a 4xx the provider rejected, a mid-stream provider error) may be after scrubbing: a multi-megabyte 400 body must not
#: land whole in the session record. Generous, because ``primer.common.context_overflow`` reads these messages.
_UNTOUCHED_MESSAGE_CAP = 4000
#: Levels of ``{"error": {...}}`` the body walk follows before it falls back to a dump; a hostile body can nest to the recursion limit.
_MAX_NESTING = 8
#: Levels and entries the fallback dump keeps of a body that has no message (it is capped to ``UPSTREAM_TEXT_CAP`` characters afterwards anyway).
_DUMP_DEPTH = 4
_DUMP_ITEMS = 16

_ELLIPSIS = "..."
_REDACTED = "[REDACTED]"
#: A configured value shorter than this is not masked (a one-, two- or three-letter "key" would blank ordinary words everywhere).
_MIN_SECRET_LENGTH = 4
#: From here up a configured value is masked wherever it appears. Below it (4 to 7 characters) it is masked as a TOKEN only: where it is not part of a
#: longer run of letters or digits, so a short key does not blank the numbers the context-overflow veto reads ("12345678" is not the key "1234").
_EVERYWHERE_LENGTH = 8
#: What a keyless local server is configured with, because the SDK insists on SOME key: masking these would blank ordinary words ("none of the results"),
#: so they are never treated as secrets, whatever their length or case. Any other configured value of 4 or more characters is.
_KEYLESS_PLACEHOLDERS = frozenset({"empty", "none", "dummy", "ollama", "lm-studio", "no-key-required", "not-needed", "sk-no-key-required"})
#: The scrub is run on at most this many characters of the NORMALISED text (the scrub is linear in what it is handed and a body can be tens of
#: megabytes); the message shows at most ``UPSTREAM_TEXT_CAP`` of it (``_UNTOUCHED_MESSAGE_CAP`` for an untouched one), so nothing past the limit could be
#: shown anyway.
_SCAN_LIMIT = 64_000
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
#: A Base URL with credentials goes on the wire as ``Authorization: Basic base64(user:password)``. The configured pair is masked exactly (below); this
#: catches a ``Basic`` token of credentials primer did NOT configure, and only one that base64-decodes to printable text containing a ``:``, so the
#: words after "Basic" in prose ("Basic authentication is required") are left alone.
_BASIC = re.compile(r"(?i)(\bbasic\s+)([A-Za-z0-9+/]{4,}={0,2})")
#: Control characters that are not whitespace (NUL, ESC, ...): a terminal or a log viewer acts on them. Stripped with ``str.translate``, which is linear and
#: fast on a body of tens of megabytes (a regex substitution took seconds on a body of NULs).
_CONTROL_TABLE = {code: None for code in (*range(0x00, 0x09), *range(0x0E, 0x1C), 0x7F)}
#: Where a SHORT secret (4 to 7 characters) may begin: not inside a longer run of letters or digits, but after an escape it may: ``%XX`` and ``\uXXXX``
#: (``Bearer%20sk-1234`` is a key after a space, not a longer token) and the escapes a Python ``repr`` of a body prints (``\n``, ``\t``, ``\x1b``).
_TOKEN_START = r"(?:(?<![A-Za-z0-9])|(?<=%[0-9A-Fa-f]{2})|(?<=\\u[0-9A-Fa-f]{4})|(?<=\\[nrtfv])|(?<=\\x[0-9A-Fa-f]{2}))"

#: What each classified shape says about the provider, as the predicate of the sentence the label starts.
_WHAT: tuple[tuple[type[PrimerError], str], ...] = (
    (ServerError, "had a server error"),
    (RateLimitError, "is rate limiting requests"),
    (AuthenticationError, "rejected the credentials"),
    (NetworkError, "could not be reached"),
)


def provider_label(provider: Any) -> str:
    """``Model provider 'lm-studio-box' (openchat/lmstudio)``: the configured id, the backend kind, and the flavor of an OpenAI-compatible one."""
    kind = provider.provider.value
    flavor = getattr(provider.config, "flavor", None)
    if flavor is not None:
        kind = f"{kind}/{flavor.value}"
    return f"Model provider {provider.id!r} ({kind})"


def _forms(secret: str) -> set[str]:
    """``secret`` as the text can show it: itself; its NORMALISED form (controls stripped, whitespace runs collapsed), because the provider's text is
    normalised before it is scrubbed; and its ESCAPED form (a tab as the two characters ``\\t``), because the SDK's message for a 4xx is the ``repr`` of the body."""
    forms = {secret, secret.encode("unicode_escape").decode("ascii")}
    normalised = " ".join(secret.translate(_CONTROL_TABLE).split())
    if len(normalised) >= _MIN_SECRET_LENGTH:
        forms.add(normalised)
    return forms


def _secrets(provider: Any) -> list[str]:
    """The values configured on ``provider`` that must never appear in a message, longest first."""
    config = provider.config
    values: set[str] = set()
    key = getattr(config, "api_key", None)
    if key is not None:
        value = key.get_secret_value()
        if len(value) >= _MIN_SECRET_LENGTH and value.casefold() not in _KEYLESS_PLACEHOLDERS:
            values.update(_forms(value))
    url = getattr(config, "url", None)
    username, password = getattr(url, "username", None) or "", getattr(url, "password", None)
    if password:
        for variant in {password, unquote(password)}:
            if len(variant) >= _MIN_SECRET_LENGTH:
                values.update(_forms(variant))
        # httpx sends the userinfo of a Base URL as ``Authorization: Basic base64(user:password)``, which a provider may echo back
        for pair in {f"{username}:{password}", f"{unquote(username)}:{unquote(password)}"}:
            token = base64.b64encode(pair.encode()).decode()
            values.update({token, token.rstrip("=")})        # a provider may echo it with or without its padding
    return sorted(values, key=len, reverse=True)


def _mask(text: str, secret: str) -> str:
    if len(secret) >= _EVERYWHERE_LENGTH:
        return text.replace(secret, _REDACTED)
    return re.sub(_TOKEN_START + re.escape(secret) + r"(?![A-Za-z0-9])", _REDACTED, text)


def _mask_basic(match: re.Match[str]) -> str:
    """``Basic <token>`` with the token masked when it is base64 of printable ``user:password``-shaped text; any other word is left as it was."""
    token = match.group(2)
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("utf-8")
    except ValueError:                                  # not base64 (binascii.Error), or not text (UnicodeDecodeError): both are ValueErrors
        return match.group(0)
    return match.group(1) + _REDACTED if ":" in decoded and decoded.isprintable() else match.group(0)


def scrub(text: str, provider: Any) -> str:
    """``text`` with the provider's configured credentials, URL-borne credentials and bearer / basic tokens masked."""
    for secret in _secrets(provider):
        text = _mask(text, secret)
    text = redact_url_secrets(text)
    text = _BEARER.sub(r"\1" + _REDACTED, text)
    return _BASIC.sub(_mask_basic, text)


_WORD = re.compile(r"\S+")


def _collapse(text: str, limit: int) -> str:
    """``" ".join(text.split())`` cut to ``limit`` characters, read lazily: it stops after the words that fill the limit instead of splitting a body of
    tens of megabytes into a list nobody will read past the first ``limit`` characters of."""
    words: list[str] = []
    size = 0
    for match in _WORD.finditer(text):
        words.append(match.group())
        size += match.end() - match.start() + 1
        if size > limit:
            break
    return " ".join(words)[:limit]


def _clean(text: str, provider: Any, *, collapse: bool) -> str:
    """``text`` normalised, THEN cut to ``_SCAN_LIMIT``, THEN scrubbed.

    Normalised first: control characters stripped (a NUL inside an echoed key must not defeat the exact match) and, for the provider's own text,
    whitespace collapsed. The slice comes after, because the message shows the first characters of the NORMALISED text: cut first, a key echoed
    behind padding that straddles the limit would be cut there and the collapse would then bring its first characters to the front, where the exact
    match could no longer see them. After normalising, anything the message can show lies well inside the prefix that is scrubbed.
    """
    text = text.translate(_CONTROL_TABLE)
    text = _collapse(text, _SCAN_LIMIT) if collapse else text[:_SCAN_LIMIT]
    return scrub(text, provider)


def _clipped(value: object, depth: int = _DUMP_DEPTH) -> object:
    """``value`` cut to ``_DUMP_DEPTH`` levels and ``_DUMP_ITEMS`` entries per container, so dumping it is bounded whatever the body nests to."""
    if depth <= 0:
        return "..."
    if isinstance(value, dict):
        return {str(k): _clipped(v, depth - 1) for k, v in list(value.items())[:_DUMP_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [_clipped(v, depth - 1) for v in list(value)[:_DUMP_ITEMS]]
    return value


def _body_message(body: object) -> str | None:
    """The provider's sentence in a response body as the openai and anthropic SDKs give it: decoded JSON, or the raw text when it was not JSON.

    Walks ``{"error": {...}}`` iteratively, at most ``_MAX_NESTING`` levels (a hostile body can nest to the recursion limit), then falls back to a
    bounded dump of what it reached.
    """
    for _ in range(_MAX_NESTING):
        if body is None:
            return None
        if isinstance(body, str):
            return body
        if not isinstance(body, dict):
            return str(body)
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
        inner = body.get("error")
        if isinstance(inner, str) and inner:
            return inner
        if not isinstance(inner, dict):
            break
        body = inner
    return json.dumps(_clipped(body), default=str)


def _upstream_text(exc: BaseException, err: PrimerError) -> str | None:
    """What the provider (or, for a network failure, the transport underneath) said, unscrubbed and uncapped."""
    if isinstance(err, NetworkError):
        # The SDK's own message is a class label ("Connection error."); the reason is on the exception it was raised from, else the exception
        # carries it itself (ollama's RequestError keeps it on ``.error``, a builtin ConnectionError in its text).
        cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)      # ``raise X from None`` suppresses it on purpose
        if cause is not None and str(cause):
            return str(cause)
        own = getattr(exc, "error", None)
        if isinstance(own, str) and own:
            return own
        return str(exc) if isinstance(exc, OSError) and str(exc) else None
    if hasattr(exc, "body"):                                    # openai, anthropic
        return _body_message(exc.body)
    ollama_error = getattr(exc, "error", None)
    if ollama_error is not None:                                # ollama.ResponseError: a string, or a decoded JSON error
        return _body_message(ollama_error)
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) else None        # google.genai.errors.APIError


def _cap(text: str, limit: int = UPSTREAM_TEXT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _rebuilt(err: PrimerError, message: str) -> PrimerError:
    return type(err)(message, code=err.code, status_code=err.status_code, cause=err.cause)


def describe_failure(err: PrimerError, exc: BaseException, provider: Any) -> PrimerError:
    """``err`` (what the classifier made of ``exc``) with the message a person needs; class, ``code``, ``status_code`` and ``cause`` are kept."""
    what = next((phrase for cls, phrase in _WHAT if isinstance(err, cls)), None)
    if what is None:
        message = _cap(_clean(err.message, provider, collapse=False), _UNTOUCHED_MESSAGE_CAP)
        return err if message == err.message else _rebuilt(err, message)
    sentence = f"{provider_label(provider)} {what}"
    if isinstance(err, NetworkError):
        sentence += f" ({type(exc).__name__})"
    elif err.status_code is not None:
        sentence += f" (HTTP {err.status_code})"
    try:
        text = _upstream_text(exc, err)
        if text:
            # Scrub FIRST, then cut: a secret cut in half by the cap would be left partly readable.
            text = _cap(_clean(text, provider, collapse=True))
    except Exception as problem:  # noqa: BLE001 -- the text is a courtesy; a body nobody can read must not turn a failed call into a crash
        # Only the type is logged: this runs inside the adapter's ``except``, so a traceback would chain the SDK exception whose text is the raw body.
        logger.warning("could not read the provider's text out of a failed call (%s); the sentence goes without it", type(problem).__name__)
        text = None
    if text:
        sentence += f": {text}"
    return _rebuilt(err, sentence)
