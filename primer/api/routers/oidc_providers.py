"""Admin CRUD over OIDC SSO providers. client_secret auto-masked by pydantic."""
from fastapi import Request

from primer.api.routers._crud import make_crud_router
from primer.api.deps import get_oidc_provider_storage
from primer.model.oidc import OidcProvider


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


oidc_providers_router = make_crud_router(
    model_cls=OidcProvider,
    storage_dep=get_oidc_provider_storage,
    plural="admin/oidc-providers",
    tag="oidc-providers",
    on_pre_update=_preserve_client_secret_if_blank,
)

__all__ = ["oidc_providers_router"]
