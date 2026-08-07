"""Resolve a model profile to the concrete facts a call needs.

The single seam every executor-build path goes through. It replaces the
former "``get_llm(provider_id)``, then scan ``provider.models`` for a
matching name" lookup that was duplicated across five call sites.

Takes ids rather than an :class:`~primer.model.agent.Agent` so the
precedence rule lives in exactly one place and the resolver stays
independent of the agent model's shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from primer.int.storage_provider import StorageProvider
from primer.model.except_ import NotFoundError
from primer.model.model_profile import ModelProfile, ModelProfileConfig


@dataclass(frozen=True)
class ResolvedModel:
    """Everything a turn needs to make a call, flattened from a profile.

    Frozen because it is threaded through executor construction and read
    at several layers; a mutable carrier here would make it unclear which
    layer owned the values.
    """

    profile_id: str
    provider_id: str
    model_name: str
    context_length: int
    config: ModelProfileConfig


async def resolve_model(
    storage_provider: StorageProvider,
    *,
    default_profile_id: str,
    override_profile_id: str | None = None,
) -> ResolvedModel:
    """Resolve the profile a turn should run under.

    ``override_profile_id`` wins when set; otherwise the agent's own
    default applies. An override naming a profile that does not exist is
    an error rather than a silent fallback to the default: the caller
    asked for a specific model and quietly running a different one would
    be worse than failing.

    Raises :class:`~primer.model.except_.NotFoundError` when the resolved
    profile is absent, which the REST layer renders as 404 and the
    executor-build path surfaces as a failed turn.
    """
    profile_id = override_profile_id or default_profile_id
    row = await storage_provider.get_storage(ModelProfile).get(profile_id)
    if row is None:
        raise NotFoundError(f"ModelProfile {profile_id!r} does not exist")
    return ResolvedModel(
        profile_id=row.id,
        provider_id=row.provider_id,
        model_name=row.model_name,
        context_length=row.context_length,
        config=row.config,
    )


__all__ = ["ResolvedModel", "resolve_model"]
