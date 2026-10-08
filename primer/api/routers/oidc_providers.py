"""Admin CRUD over OIDC SSO providers. client_secret auto-masked by pydantic."""
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from primer.api.routers._crud import make_crud_router
from primer.api.deps import get_oidc_provider_storage
from primer.auth import oidc
from primer.model.oidc import OidcProvider


def _refuse(error: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": error, "message": message})


def _check_discovery_url_shape(entity: OidcProvider) -> None:
    """A discovery URL is a full http(s) URL. Checked on every save, enabled or not, and without any network call.

    The URL is trimmed first (and stored trimmed). ``urlsplit`` itself raises ``ValueError`` on a malformed bracketed host
    (``https://[idp.example.com/x``), which would be a 500; it is refused like any other malformed URL."""
    entity.discovery_url = (entity.discovery_url or "").strip()
    try:
        parts = urlsplit(entity.discovery_url)
        well_formed = parts.scheme in ("http", "https") and bool(parts.netloc)
    except ValueError:
        well_formed = False
    if not well_formed:
        raise _refuse(
            "discovery_url_invalid",
            "The discovery URL must be a full http(s) URL, for example "
            "https://idp.example.com/.well-known/openid-configuration.",
        )


async def _check_discoverable(discovery_url: str) -> None:
    """The login flow fetches this document on every sign-in. Fetch it now, through the same code, so a provider that cannot
    answer is refused when it is enabled and never shows up as a "Sign in with ..." button that ends on a JSON error page."""
    try:
        await oidc.discover(discovery_url)
    except oidc.OidcError as exc:
        raise _refuse("discovery_failed", str(exc)) from exc


async def _on_pre_create(entity: OidcProvider, request: Request) -> None:
    _check_discovery_url_shape(entity)
    if entity.enabled:
        await _check_discoverable(entity.discovery_url)


async def _preserve_client_secret_if_blank(
    entity: OidcProvider, existing: OidcProvider, request: Request,
) -> None:
    """PUT is a full replace (``make_crud_router`` validates the raw wire
    dict as a complete :class:`OidcProvider`) and ``client_secret`` is
    optional -- a caller that omits the key (or sends ``null``) would
    otherwise silently clear a previously-configured secret. The admin
    console (Task 9) treats ``client_secret`` as write-only in its
    create/edit modal and never round-trips the masked ``"**********"``
    placeholder GET/list returns, so "the field came back ``None``" means
    "the admin left it blank", not "the admin wants to clear it" --
    preserve the existing value in that case. An explicit new secret
    still overwrites normally.

    A *non-UI* caller (or a naive script) commonly does a raw
    read-modify-write: ``GET`` a provider, get back the masked
    ``client_secret: "**********"`` placeholder, then ``PUT`` that body
    back verbatim. Pydantic's default JSON dump for a :class:`SecretStr`
    always redacts to the fixed 10-asterisk literal ``"**********"``
    regardless of the underlying value's length (confirmed against
    ``pydantic==2.13.4`` here and against
    ``test_admin_create_and_list_masks_client_secret`` /
    ``test_put_without_client_secret_preserves_existing``, both of which
    assert the masked response body is exactly that literal) -- so that
    round-trip does *not* come back as ``None``, it comes back as the
    mask string itself, and would otherwise sail past the ``is None``
    check below and get persisted by ``dump_for_storage`` as the "real"
    secret, corrupting it. Treat the mask literal (and an explicit empty
    string, which a form might also send for "unchanged") the same as
    "blank" -- preserve the existing stored secret. A genuinely new
    secret is, by construction, exceedingly unlikely to equal either
    sentinel and still replaces the stored value normally.
    """
    incoming = entity.client_secret
    is_blank = incoming is None or incoming.get_secret_value() in ("", "**********")
    if is_blank and existing.client_secret is not None:
        entity.client_secret = existing.client_secret


async def _on_pre_update(entity: OidcProvider, existing: OidcProvider, request: Request) -> None:
    await _preserve_client_secret_if_blank(entity, existing, request)
    _check_discovery_url_shape(entity)
    # Only the changes that can put a broken button on the login screen refetch: turning a provider on, or pointing an enabled one at
    # another URL. An unrelated edit (a rename) must not depend on the IdP being up.
    if entity.enabled and (not existing.enabled or entity.discovery_url != existing.discovery_url):
        await _check_discoverable(entity.discovery_url)


oidc_providers_router = make_crud_router(
    model_cls=OidcProvider,
    storage_dep=get_oidc_provider_storage,
    plural="admin/oidc-providers",
    tag="oidc-providers",
    on_pre_create=_on_pre_create,
    on_pre_update=_on_pre_update,
)

__all__ = ["oidc_providers_router"]
