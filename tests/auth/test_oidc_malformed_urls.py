"""A URL httpx cannot even build a request for must come back from ``primer.auth.oidc`` as an ``OidcError``, never as a raw httpx or idna error.

``discover`` (the admin's discovery URL), ``fetch_jwks`` (the ``jwks_uri`` the IdP's own document names) and ``exchange_code`` (its
``token_endpoint``) all catch ``httpx.HTTPError`` around the request, and that is not what a URL httpx refuses to build raises:

* ``httpx.InvalidURL`` (a non-numeric port, ``host:80:90``, a bracketed host that never closes, a NUL byte) is a plain ``Exception``;
* the IDNA codec raises ``idna.IDNAError`` (a ``UnicodeError``, so a ``ValueError``) for a malformed internationalised host (``xn--``).

Either one escaped the router that called it (``/v1/admin/oidc-providers``) and the login flow (``/v1/auth/sso/<id>/login``) as a 500
instead of the 422 / 502 each of them gives for any other unreachable IdP. No network is touched: the request is never built (and
``respx`` would fail the test loudly if one went out).
"""

from __future__ import annotations

import pytest
import respx

from primer.auth import oidc
from primer.model.oidc import OidcProvider

# The exceptions that escaped before: InvalidURL x4 (port, host:port:port, unclosed bracket, NUL) and IDNAError.
BAD_URLS = [
    "https://idp.example.com:abc/.well-known/openid-configuration",
    "https://idp.example.com:80:90/x",
    "https://[::1/x",
    "https://idp.example.com/\x00x",
    "https://xn--/x",
]


def _provider() -> OidcProvider:
    return OidcProvider(
        name="Test IdP", discovery_url="https://idp.example.com/.well-known/openid-configuration", client_id="cid", client_secret=None,
    )


def _metadata(**overrides) -> oidc.OidcMetadata:
    fields = {
        "issuer": "https://idp.example.com/",
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/jwks.json",
        "id_token_signing_algs": ["RS256"],
    }
    fields.update(overrides)
    return oidc.OidcMetadata(**fields)


@respx.mock
@pytest.mark.parametrize("url", BAD_URLS)
async def test_discover_turns_an_unbuildable_url_into_an_oidc_error(url: str) -> None:
    with pytest.raises(oidc.OidcError) as caught:
        await oidc.discover(url)

    assert "discovery request" in str(caught.value)


@respx.mock
@pytest.mark.parametrize("url", BAD_URLS)
async def test_fetch_jwks_turns_an_unbuildable_url_into_an_oidc_error(url: str) -> None:
    """The ``jwks_uri`` comes from the IdP's own discovery document, so a hostile or broken IdP can hand over any string."""
    with pytest.raises(oidc.OidcError) as caught:
        await oidc.fetch_jwks(url)

    assert "JWKS request" in str(caught.value)


@respx.mock
@pytest.mark.parametrize("url", BAD_URLS)
async def test_exchange_code_turns_an_unbuildable_token_endpoint_into_an_oidc_error(url: str) -> None:
    with pytest.raises(oidc.OidcError) as caught:
        await oidc.exchange_code(
            metadata=_metadata(token_endpoint=url),
            provider=_provider(),
            code="c",
            code_verifier="v",
            redirect_uri="https://app.example.com/callback",
        )

    assert "token exchange request failed" in str(caught.value)
