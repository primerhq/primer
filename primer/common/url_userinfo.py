"""The password of a URL's userinfo: masking it, and putting it back (ticket 01a11cdf part 3, option A; lead rulings of 2026-10-09).

A provider's Base URL may carry ``user:password@`` (an operator's reverse proxy): httpx sends it as Basic auth and pydantic accepts it, but it is a credential that every served copy of the
row (API responses, CRUD events, tool results) used to carry in clear next to a masked ``api_key``. This module is a LEAF (it imports nothing from primer) and knows only the text:

* :func:`mask_userinfo`: what every JSON-mode dump of such a URL writes. The scheme, host, port, path, query and fragment are never touched. The username stays visible ONLY when a password
  is present (it tells an operator which account is configured, and is not the secret); a lone userinfo (``https://TOKEN@host``, the personal-access-token-in-the-username shape, and
  ``user:@host`` whose empty password makes the username the only candidate secret) is masked whole. An ``@`` after the authority (a path, a query, a fragment) is not userinfo, and
  neither is text that is not ``scheme://...``.
* :func:`restore_userinfo`: what a full-replace PUT of the served body uses to put the STORED credential back, as ``preserve_masked_secrets`` does for a ``SecretStr``.

The userinfo runs to the LAST ``@`` of the authority (``http://svc:p@ss@host/`` has the password ``p@ss``) and the password is everything after the FIRST ``:``.
"""

from __future__ import annotations

import re

MASK = "**********"

# scheme://authority + the rest. The authority runs to the first "/", "?" or "#".
_URL = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<authority>[^/?#]*)(?P<rest>.*)", re.DOTALL)


def _split(url: str) -> tuple[str, str, str, str] | None:
    """``(scheme, userinfo, host_and_port, rest)`` of a URL that has userinfo, else ``None``."""
    found = _URL.fullmatch(url)
    if found is None:
        return None
    authority = found.group("authority")
    at = authority.rfind("@")
    if at < 0:
        return None
    return found.group("scheme"), authority[:at], authority[at + 1 :], found.group("rest")


def mask_userinfo(url: str) -> str:
    """``url`` with the secret of its userinfo replaced by :data:`MASK`; any other text is returned as it is. Masking twice is masking once."""
    parts = _split(url)
    if parts is None:
        return url
    scheme, userinfo, host, rest = parts
    user, colon, password = userinfo.partition(":")
    if colon and password:
        masked = f"{user}:{MASK}"
    elif userinfo:
        masked = MASK
    else:
        return url
    return f"{scheme}://{masked}@{host}{rest}"


def restore_userinfo(incoming: str, stored: str) -> str | None:
    """``incoming`` with the STORED credential put back, when ``incoming`` is the mask :func:`mask_userinfo` served for ``stored``; ``None`` for anything else (the caller leaves it as sent).

    ``user:**********`` is restored when the stored URL has a password and the same username; a lone ``**********`` when the stored URL is a lone userinfo. Everything else of ``incoming``
    (scheme, host, port, path, query) is the person's, so changing the host and leaving the mask alone keeps the password. A different username, a stored URL that never held a
    credential, a real new password and a removed credential are all the person's change and come back ``None``.
    """
    sent, kept = _split(incoming), _split(stored)
    if sent is None or kept is None:
        return None
    scheme, sent_userinfo, host, rest = sent
    kept_user, kept_colon, kept_password = kept[1].partition(":")
    sent_user, sent_colon, sent_password = sent_userinfo.partition(":")
    if sent_colon and sent_password == MASK:
        if not (kept_colon and kept_password) or kept_user != sent_user:
            return None
    elif sent_userinfo == MASK:
        if kept_colon or not kept[1] or kept[1] == MASK:
            return None
    else:
        return None
    return f"{scheme}://{kept[1]}@{host}{rest}"


__all__ = ["MASK", "mask_userinfo", "restore_userinfo"]
