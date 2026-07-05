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
    """
    if entity.client_secret is None and existing.client_secret is not None:
        entity.client_secret = existing.client_secret


oidc_providers_router = make_crud_router(
    model_cls=OidcProvider,
    storage_dep=get_oidc_provider_storage,
    plural="admin/oidc-providers",
    tag="oidc-providers",
    on_pre_update=_preserve_client_secret_if_blank,
)

__all__ = ["oidc_providers_router"]
