"""The origin a credential is sent to: scheme, host and port (ticket 01a1212a).

A stored secret that a GET served masked is put back by a full-replace PUT (:func:`primer.model.common.preserve_masked_secrets`) only while the config still points at the origin the secret
was stored for; this module is the comparison. It is a LEAF (it imports nothing from primer) and knows only the text.

Two URLs have the same origin when their scheme, host and port are equal after the normalisation a client would do anyway: the scheme and the host are case-insensitive, and the default
port of the scheme (80 for ``http`` / ``ws``, 443 for ``https`` / ``wss``) is the same as no port. The userinfo, path, query and fragment are not part of an origin. A value that is not
``scheme://host...`` (a typo, a bare word) is its own origin, compared as the text: unchanged keeps the secret, any change is a move. So is a value whose AUTHORITY (the part between
``://`` and the first ``/``, ``?`` or ``#``) holds a backslash, whitespace or a control character: ``urlsplit`` and the client that makes the request read such a host differently
(``https://attacker.example\\@home.example`` is ``home.example`` to one and ``attacker.example`` to the other), so neither reading is trusted. The authority is judged TWICE, because a
scan for the first ``://`` can be steered to the wrong one: a tab, CR or LF in the scheme separator (``https:<TAB>//attacker.example\\@home.example/x://y``) puts the first ``://`` in the path,
while ``urlsplit`` drops the control character and reads ``home.example`` and a WHATWG client (``HttpUrl``) reads ``attacker.example`` (review of #711, round 2, N4b). So the netloc ``urlsplit``
itself read is checked for the same characters, and a value that holds a tab, CR or LF anywhere is compared as text.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

Origin = tuple[str, ...]


def _ambiguous_authority(text: str) -> bool:
    """True when the authority of ``scheme://authority/...`` holds a character that parsers disagree about: a backslash, whitespace or a control character."""
    _, sep, rest = text.partition("://")
    if not sep:
        return False
    for end, ch in enumerate(rest):
        if ch in "/?#":
            rest = rest[:end]
            break
    return any(ch == "\\" or ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in rest)


def _ambiguous_chars(text: str) -> bool:
    return any(ch == "\\" or ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text)


def origin_of(url: str) -> Origin:
    """``("url", scheme, host, port)`` for a URL with a host, else ``("text", url)``; two values with the same origin compare equal."""
    text = url.strip()
    if _ambiguous_authority(text) or any(ch in text for ch in "\t\r\n"):
        return ("text", text)
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
        netloc = parts.netloc
    except ValueError:
        return ("text", text)
    if _ambiguous_chars(netloc):
        return ("text", text)          # the authority urlsplit itself read: a scan for the first :// may have looked at another
    if not parts.scheme or "://" not in text or not host:
        return ("text", text)
    scheme = parts.scheme.lower()
    return ("url", scheme, host.lower(), str(port if port is not None else _DEFAULT_PORTS.get(scheme, "")))


__all__ = ["Origin", "origin_of"]
