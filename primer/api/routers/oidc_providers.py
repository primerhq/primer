"""Admin CRUD over OIDC SSO providers. client_secret auto-masked by pydantic."""
from primer.api.routers._crud import make_crud_router
from primer.api.deps import get_oidc_provider_storage
from primer.model.oidc import OidcProvider

oidc_providers_router = make_crud_router(
    model_cls=OidcProvider,
    storage_dep=get_oidc_provider_storage,
    plural="admin/oidc-providers",
    tag="oidc-providers",
)

__all__ = ["oidc_providers_router"]
