"""The password of a URL's userinfo: masking it, and putting it back (ticket 01a11cdf part 3, option A; lead rulings of 2026-10-09).

A provider's Base URL may carry ``user:password@`` (an operator's reverse proxy): httpx sends it as Basic auth and pydantic accepts it, but it is a credential that every served copy of the
row (API responses, CRUD events, tool results) used to carry in clear next to a masked ``api_key``. This module is a LEAF (it imports nothing from primer) and knows only the text:

* :func:`mask_userinfo`: what every JSON-mode dump of such a URL writes. The scheme, host, port, path, query and fragment are never touched. The username stays visible ONLY when a password
  is present (it tells an operator which account is configured, and is not the secret); a lone userinfo (``https://TOKEN@host``, the personal-access-token-in-the-username shape, and
  ``user:@host`` whose empty password makes the username the only candidate secret) is masked whole. An ``@`` after the authority (a path, a query, a fragment) is not userinfo, and
  neither is text that is not ``scheme://...``.
* :func:`restore_userinfo`: what a full-replace PUT of the served body uses to put the STORED credential back, as ``preserve_masked_secrets`` does for a ``SecretStr``, but ONLY for
  the origin (scheme, host, port) and the user the mask was served for: an update that moves the URL to another host with the mask left alone is refused, not given the password.
  Known limit: the username stays visible when a password is present, so a token kept in the username slot with a dummy password
  (``https://<token>:x-oauth-basic@github.com/``) shows the token; only a lone userinfo is masked whole.

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


class MaskCannotBeRestored(ValueError):
    """A URL carries the mask :func:`mask_userinfo` serves, but the stored URL cannot give the credential back to it.

    The text says why in words that name no credential: the origin (scheme, host, port) or the user is not the one the mask was served for, the stored URL holds no credential of
    that kind, or there is no stored URL at all. The caller refuses the update (a 422): the literal mask must not be stored as the password, and the stored credential must not be
    handed to a host the person controls.
    """


def carries_mask(url: str) -> bool:
    """True when the userinfo of ``url`` is the mask :func:`mask_userinfo` serves: ``user:**********`` or a lone ``**********``."""
    parts = _split(url)
    if parts is None:
        return False
    user, colon, password = parts[1].partition(":")
    return (bool(colon) and password == MASK) or parts[1] == MASK


def restore_userinfo(incoming: str, stored: str) -> str | None:
    """``incoming`` with the STORED credential put back, when ``incoming`` carries the mask :func:`mask_userinfo` served for ``stored``; ``None`` when it carries no mask.

    A mask is restored ONLY for the URL it was served for: the scheme, host and port equal the stored URL's, and ``user:**********`` finds the same username with a password stored
    (a lone ``**********`` finds a lone userinfo). Everything after the authority (path, query, fragment) is the person's, so editing it with the mask left alone keeps the password.
    A mask that cannot be restored, because the origin or the user changed, the stored URL has no credential of that kind, or it is not a URL, raises :class:`MaskCannotBeRestored`:
    restoring it would give the stored credential to whatever host the update names, and leaving it would store the literal mask as the password. A real new password and a
    removed credential carry no mask and come back ``None``: they are the person's change.
    """
    sent = _split(incoming)
    if sent is None or not carries_mask(incoming):
        return None
    scheme, sent_userinfo, host, rest = sent
    kept = _split(stored)
    if kept is None:
        raise MaskCannotBeRestored("the stored URL holds no credential to put back")
    if (scheme.lower(), host.lower()) != (kept[0].lower(), kept[2].lower()):
        raise MaskCannotBeRestored("the scheme, host or port changed: the stored credential is kept only for the URL it belongs to")
    kept_user, kept_colon, kept_password = kept[1].partition(":")
    sent_user, sent_colon, _ = sent_userinfo.partition(":")
    if sent_colon:
        if not (kept_colon and kept_password) or kept_user != sent_user:
            raise MaskCannotBeRestored("the stored URL has no password for this user")
    elif kept_colon or not kept[1] or kept[1] == MASK:
        raise MaskCannotBeRestored("the stored URL has no lone credential to put back")
    return f"{scheme}://{kept[1]}@{host}{rest}"


__all__ = ["MASK", "MaskCannotBeRestored", "carries_mask", "mask_userinfo", "restore_userinfo"]
