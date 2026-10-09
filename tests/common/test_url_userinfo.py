"""Masking the password of a URL's userinfo, and restoring it (ticket 01a11cdf part 3, option A; lead rulings of 2026-10-09).

A provider's Base URL may carry ``user:password@`` (an operator's reverse proxy): httpx sends it as Basic auth, pydantic accepts it, and the API served it back in clear next to a
masked ``api_key``. ``mask_userinfo`` is what every JSON-mode dump of such a URL writes; ``restore_userinfo`` is what a full-replace PUT of the served body uses to put the stored
credential back (the way ``preserve_masked_secrets`` does for a ``SecretStr``).

The rules, from the lead:

* the username stays visible ONLY when a password is present (it tells an operator which account is configured, and is not the secret);
* a lone userinfo (``https://TOKEN@host``: the classic personal-access-token-in-the-username shape) is masked whole;
* scheme, host, port, path, query and fragment are never touched, and an ``@`` after the authority is not userinfo.
"""

from __future__ import annotations

import pytest

from primer.common.url_userinfo import MASK, mask_userinfo, restore_userinfo


@pytest.mark.parametrize(
    ("url", "masked"),
    [
        ("http://svc:s3cr3t@host/v1", f"http://svc:{MASK}@host/v1"),
        ("https://svc:s3cr3t@host.example:8443/a/b?x=1#frag", f"https://svc:{MASK}@host.example:8443/a/b?x=1#frag"),
        ("http://svc:p%40ss%2Fword@host/", f"http://svc:{MASK}@host/"),
        ("http://svc:a:b:c@host/", f"http://svc:{MASK}@host/"),
        ("http://svc:p@ss@host/", f"http://svc:{MASK}@host/"),                       # a raw @ in the password: userinfo runs to the LAST @ of the authority
        ("http://svc:pa'ss@host/", f"http://svc:{MASK}@host/"),
        ("http://u:p@[::1]:8080/v1", f"http://u:{MASK}@[::1]:8080/v1"),
        ("http://:onlypw@host/", f"http://:{MASK}@host/"),                          # an empty username with a password
        ("HTTPS://svc:pw@Host/", f"HTTPS://svc:{MASK}@Host/"),
        ("postgresql+asyncpg://svc:pw@db/x", f"postgresql+asyncpg://svc:{MASK}@db/x"),
    ],
)
def test_the_password_is_masked_and_the_username_stays(url: str, masked: str) -> None:
    assert mask_userinfo(url) == masked


@pytest.mark.parametrize(
    ("url", "masked"),
    [
        ("https://ghp_abcdefghij@github.com/org/repo.git", f"https://{MASK}@github.com/org/repo.git"),
        ("https://TOKEN@host/", f"https://{MASK}@host/"),
        ("https://user:@host/", f"https://{MASK}@host/"),                            # an empty password is no password: the username may be the secret
        ("https://user@example.com", f"https://{MASK}@example.com"),                 # looks like an account name: it may be a token, so it goes
    ],
)
def test_a_lone_userinfo_is_masked_whole(url: str, masked: str) -> None:
    assert mask_userinfo(url) == masked


@pytest.mark.parametrize(
    "url",
    [
        "http://host/v1",
        "http://host:8080/v1",
        "http://host/v1?mail=a@b.example",                                           # an @ in the query is not userinfo
        "http://host/a@b",                                                           # nor one in the path
        "http://host/#a@b",
        "not a url",
        "",
        "//u:p@host/",                                                               # no scheme: not a URL this helper reads
        "mailto:user@example.com",
    ],
)
def test_text_without_userinfo_is_left_alone(url: str) -> None:
    assert mask_userinfo(url) == url


def test_masking_twice_is_masking_once() -> None:
    for url in ("http://svc:s3cr3t@host/v1", "https://TOKEN@host/", "http://host/"):
        once = mask_userinfo(url)
        assert mask_userinfo(once) == once


@pytest.mark.parametrize(
    ("incoming", "stored", "restored"),
    [
        # the served body comes back unchanged
        (f"http://svc:{MASK}@host/v1", "http://svc:s3cr3t@host/v1", "http://svc:s3cr3t@host/v1"),
        # the host or path changed and the password was left alone
        (f"http://svc:{MASK}@other.example:9000/v2", "http://svc:s3cr3t@host/v1", "http://svc:s3cr3t@other.example:9000/v2"),
        # a percent-encoded password is restored as stored
        (f"http://svc:{MASK}@host/", "http://svc:p%40ss@host/", "http://svc:p%40ss@host/"),
        # a lone userinfo
        (f"https://{MASK}@github.com/org/repo.git", "https://ghp_abcdefghij@github.com/org/repo.git", "https://ghp_abcdefghij@github.com/org/repo.git"),
    ],
)
def test_a_served_mask_gets_the_stored_credential_back(incoming: str, stored: str, restored: str) -> None:
    assert restore_userinfo(incoming, stored) == restored


@pytest.mark.parametrize(
    ("incoming", "stored"),
    [
        (f"http://other:{MASK}@host/v1", "http://svc:s3cr3t@host/v1"),               # another username: not what was served for this row
        (f"http://svc:{MASK}@host/v1", "http://host/v1"),                            # the stored URL never held a credential
        (f"http://svc:{MASK}@host/v1", "https://TOKEN@host/v1"),                     # the stored one was a lone userinfo
        (f"https://{MASK}@host/", "http://svc:s3cr3t@host/"),                        # an incoming lone mask for a stored user:password
        ("http://svc:newpass@host/v1", "http://svc:s3cr3t@host/v1"),                 # a real new password is the person's change
        ("http://host/v1", "http://svc:s3cr3t@host/v1"),                             # the credential was removed on purpose
        ("not a url", "http://svc:s3cr3t@host/"),
    ],
)
def test_anything_else_is_left_as_sent(incoming: str, stored: str) -> None:
    assert restore_userinfo(incoming, stored) is None
