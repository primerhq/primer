"""``GET`` / ``PUT /v1/{plural}/{id}/access`` of grantable infrastructure (access-control P4 Task 3, spec 6.1, 6.2).

Model profiles and the providers a user-owned entity references carry an admin-set use grant. The sub-resource is mounted on the USER tier
(unlike the admin CRUD routers): :func:`primer.access.service.set_access` (``use_only``) decides, so a grantee reads the view, a stranger
answers 404, and only an admin can change it. Include these BEFORE the CRUD routers.
"""

from __future__ import annotations

from fastapi import APIRouter

from primer.api.deps import (
    get_cross_encoder_provider_storage,
    get_embedding_provider_storage,
    get_llm_provider_storage,
    get_model_profile_storage,
    get_semantic_search_storage,
    get_workspace_provider_storage,
)
from primer.api.routers.access import make_access_router
from primer.api.routers.channels import _get_channel_provider_storage
from primer.model.channel import ChannelProvider
from primer.model.model_profile import ModelProfile
from primer.model.providers.cross_encoder import CrossEncoderProvider
from primer.model.providers.embedding import EmbeddingProvider
from primer.model.providers.llm import LLMProvider
from primer.model.providers.vector import SemanticSearchProvider
from primer.model.workspace import WorkspaceProvider

#: (model, plural of its CRUD router, its storage dependency)
_KINDS = (
    (ModelProfile, "model_profiles", get_model_profile_storage),
    (LLMProvider, "llm_providers", get_llm_provider_storage),
    (EmbeddingProvider, "embedding_providers", get_embedding_provider_storage),
    (CrossEncoderProvider, "cross_encoder_providers", get_cross_encoder_provider_storage),
    (SemanticSearchProvider, "ssp", get_semantic_search_storage),
    (WorkspaceProvider, "workspace_providers", get_workspace_provider_storage),
    (ChannelProvider, "channel_providers", _get_channel_provider_storage),
)

infra_access_routers: list[APIRouter] = [
    make_access_router(model_cls=model, plural=plural, storage_dep=dep, tag=plural, use_only=True) for model, plural, dep in _KINDS
]

__all__ = ["infra_access_routers"]
