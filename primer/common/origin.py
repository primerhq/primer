"""The origin a credential is sent to: scheme, host and port (ticket 01a1212a).

A stored secret that a GET served masked is put back by a full-replace PUT (:func:`primer.model.common.preserve_masked_secrets`) only while the config still points at the origin the secret
was stored for; this module is the comparison. It is a LEAF (it imports nothing from primer) and knows only the text.

Two URLs have the same origin when their scheme, host and port are equal after the normalisation a client would do anyway: the scheme and the host are case-insensitive, and the default
port of the scheme (80 for ``http`` / ``ws``, 443 for ``https`` / ``wss``) is the same as no port. The userinfo, path, query and fragment are not part of an origin. A value that is not
``scheme://host...`` (a typo, a bare word) is its own origin, compared as the text: unchanged keeps the secret, any change is a move.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

Origin = tuple[str, ...]


def origin_of(url: str) -> Origin:
    """``("url", scheme, host, port)`` for a URL with a host, else ``("text", url)``; two values with the same origin compare equal."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ("text", text)
    if not parts.scheme or "://" not in text or not host:
        return ("text", text)
    scheme = parts.scheme.lower()
    return ("url", scheme, host.lower(), str(port if port is not None else _DEFAULT_PORTS.get(scheme, "")))


__all__ = ["Origin", "origin_of"]
