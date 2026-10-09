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
Base URL password (exact values: the strongest rule, and the one that catches a key echoed back in a body), URL-borne credentials
(``primer.common.log.redact_url_secrets``) and ``Bearer`` tokens.
"""

from __future__ import annotations

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
#: An exact-value mask below these lengths would mask ordinary words (placeholder keys such as ``EMPTY``, ``none`` or ``dummy`` that a local
#: server accepts), including the numbers the context-overflow veto reads. Real API keys are far longer. URL-borne credentials are masked by
#: ``redact_url_secrets`` whatever their length.
_MIN_KEY_LENGTH = 8
_MIN_PASSWORD_LENGTH = 6
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
#: Control characters that are not whitespace (NUL, ESC, ...): a terminal or a log viewer acts on them.
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1b\x7f]")

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


def _secrets(provider: Any) -> list[str]:
    """The values configured on ``provider`` that must never appear in a message, longest first."""
    config = provider.config
    values: list[str] = []
    key = getattr(config, "api_key", None)
    if key is not None and len(key.get_secret_value()) >= _MIN_KEY_LENGTH:
        values.append(key.get_secret_value())
    password = getattr(getattr(config, "url", None), "password", None)
    if password:
        values.extend(v for v in {password, unquote(password)} if len(v) >= _MIN_PASSWORD_LENGTH)
    return sorted(set(values), key=len, reverse=True)


def scrub(text: str, provider: Any) -> str:
    """``text`` with the provider's configured credentials, URL-borne credentials and bearer tokens masked."""
    for secret in _secrets(provider):
        text = text.replace(secret, _REDACTED)
    text = redact_url_secrets(text)
    return _BEARER.sub(r"\1" + _REDACTED, text)


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
        cause = exc.__cause__ or exc.__context__
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
        message = _cap(scrub(err.message, provider), _UNTOUCHED_MESSAGE_CAP)
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
            text = _cap(" ".join(_CONTROL.sub("", scrub(text, provider)).split()))
    except Exception:  # noqa: BLE001 -- the text is a courtesy; a body nobody can read must not turn a failed call into a crash
        logger.warning("could not read the provider's text out of a failed call; the sentence goes without it", exc_info=True)
        text = None
    if text:
        sentence += f": {text}"
    return _rebuilt(err, sentence)
