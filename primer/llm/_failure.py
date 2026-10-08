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

__all__ = ["UPSTREAM_TEXT_CAP", "describe_failure", "provider_label", "scrub"]

#: Longest the provider's own text may be in a failure message (the console shows the message as the card's detail, and a 502 from a proxy can be
#: a whole HTML page).
UPSTREAM_TEXT_CAP = 300

_ELLIPSIS = "..."
_REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 4                      # a shorter "secret" would mask ordinary words
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")

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
    if key is not None:
        values.append(key.get_secret_value())
    url = getattr(config, "url", None)
    password = getattr(url, "password", None)
    if password:
        values.extend({password, unquote(password)})
    return sorted({v for v in values if len(v) >= _MIN_SECRET_LENGTH}, key=len, reverse=True)


def scrub(text: str, provider: Any) -> str:
    """``text`` with the provider's configured credentials, URL-borne credentials and bearer tokens masked."""
    for secret in _secrets(provider):
        text = text.replace(secret, _REDACTED)
    text = redact_url_secrets(text)
    return _BEARER.sub(r"\1" + _REDACTED, text)


def _body_message(body: object) -> str | None:
    """The provider's sentence in a response body as the openai and anthropic SDKs give it: decoded JSON, or the raw text when it was not JSON."""
    if body is None:
        return None
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
        inner = body.get("error")
        if isinstance(inner, str) and inner:
            return inner
        if isinstance(inner, dict):
            return _body_message(inner)
        return json.dumps(body, default=str)
    return str(body)


def _upstream_text(exc: BaseException, err: PrimerError) -> str | None:
    """What the provider (or, for a network failure, the transport underneath) said, unscrubbed and uncapped."""
    if isinstance(err, NetworkError):
        # The SDK's own message is a class label ("Connection error."); the reason is on the exception it was raised from.
        cause = exc.__cause__ or exc.__context__
        return str(cause) if cause is not None and str(cause) else None
    if hasattr(exc, "body"):                                    # openai, anthropic
        return _body_message(exc.body)
    ollama_error = getattr(exc, "error", None)
    if isinstance(ollama_error, str):                           # ollama.ResponseError
        return ollama_error
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) else None        # google.genai.errors.APIError


def _cap(text: str) -> str:
    if len(text) <= UPSTREAM_TEXT_CAP:
        return text
    return text[: UPSTREAM_TEXT_CAP - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _rebuilt(err: PrimerError, message: str) -> PrimerError:
    return type(err)(message, code=err.code, status_code=err.status_code, cause=err.cause)


def describe_failure(err: PrimerError, exc: BaseException, provider: Any) -> PrimerError:
    """``err`` (what the classifier made of ``exc``) with the message a person needs; class, ``code``, ``status_code`` and ``cause`` are kept."""
    what = next((phrase for cls, phrase in _WHAT if isinstance(err, cls)), None)
    if what is None:
        message = scrub(err.message, provider)
        return err if message == err.message else _rebuilt(err, message)
    sentence = f"{provider_label(provider)} {what}"
    if isinstance(err, NetworkError):
        sentence += f" ({type(exc).__name__})"
    elif err.status_code is not None:
        sentence += f" (HTTP {err.status_code})"
    text = _upstream_text(exc, err)
    if text:
        text = _cap(" ".join(scrub(text, provider).split()))
        if text:
            sentence += f": {text}"
    return _rebuilt(err, sentence)
