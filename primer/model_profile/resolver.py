"""Resolve a model profile to the concrete facts a call needs.

The single seam every executor-build path goes through. It replaces the
former "``get_llm(provider_id)``, then scan ``provider.models`` for a
matching name" lookup that was duplicated across five call sites.

Takes ids rather than an :class:`~primer.model.agent.Agent` so the
precedence rule lives in exactly one place and the resolver stays
independent of the agent model's shape.

Two entry points:

* :func:`resolve_model` -- facts only, no adapter construction. Used by
  callers that never build an ``LLM`` (e.g. a context-length lookup for
  compaction).
* :func:`resolve_llm` -- the (LLM, ResolvedModel) pair every execution
  path needs (matches the ``llm_resolver`` interface already declared at
  ``primer/graph/base.py:175``). Every caller that used to do the old
  ``resolve_model`` then ``provider_registry.get_llm(resolved.
  provider_id)`` two-step goes through this instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from primer.int.storage_provider import StorageProvider
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.model_profile import ModelProfile, ModelProfileConfig

if TYPE_CHECKING:
    from primer.api.registries.provider_registry import ProviderRegistry
    from primer.int.llm import LLM


@dataclass(frozen=True)
class ResolvedModel:
    """Everything a turn needs to make a call, flattened from a profile.

    Frozen because it is threaded through executor construction and read
    at several layers; a mutable carrier here would make it unclear which
    layer owned the values.

    For an aggregated profile (``kind == "aggregated"``), ``provider_id``
    and ``model_name`` are ``None`` -- there is no single provider/model
    to report, and fabricating one would misrepresent which member
    actually served a call. This is deliberate: a caller that only goes
    through :func:`resolve_model` (not :func:`resolve_llm`) and then
    tries to build an adapter from ``provider_id`` fails loud at
    ``get_llm(None)`` rather than silently resolving one arbitrary
    member. ``context_length`` is always a concrete int: for "single" it
    is the profile's own value; for "aggregated" it is the MIN over the
    resolved members' context lengths, so a caller never overpromises the
    window.
    """

    profile_id: str
    provider_id: str | None
    model_name: str | None
    context_length: int
    config: ModelProfileConfig


async def _flatten(storage, row: ModelProfile) -> ResolvedModel:
    if row.kind == "single":
        return ResolvedModel(
            profile_id=row.id,
            provider_id=row.provider_id,
            model_name=row.model_name,
            context_length=row.context_length,
            config=row.config,
        )
    # aggregated: no single provider/model to report (see ResolvedModel's
    # docstring); context_length is the MIN over members so a caller
    # never overpromises the window.
    assert row.members is not None
    member_lengths: list[int] = []
    for member_id in row.members:
        member = await storage.get(member_id)
        if member is None:
            raise NotFoundError(
                f"ModelProfile {row.id!r} member {member_id!r} does not exist"
            )
        if member.kind != "single" or member.context_length is None:
            # CRUD-time validation on the model_profiles router rejects a
            # non-single member eagerly (v1: no nested aggregation), so
            # this should be unreachable in normal operation. Raising
            # rather than recursing keeps that invariant enforced here
            # too instead of silently computing a wrong number if it is
            # ever violated (e.g. by a future write path that forgets
            # the check).
            raise ConfigError(
                f"ModelProfile {row.id!r} member {member_id!r} is not a "
                f"single-kind profile; nested aggregation should have "
                f"been rejected when {row.id!r} was written"
            )
        member_lengths.append(member.context_length)
    return ResolvedModel(
        profile_id=row.id,
        provider_id=None,
        model_name=None,
        context_length=min(member_lengths),
        config=row.config,
    )


async def resolve_model(
    storage_provider: StorageProvider,
    *,
    default_profile_id: str,
    override_profile_id: str | None = None,
) -> ResolvedModel:
    """Resolve the profile a turn should run under, facts only.

    ``override_profile_id`` wins when set; otherwise the agent's own
    default applies. An override naming a profile that does not exist is
    an error rather than a silent fallback to the default: the caller
    asked for a specific model and quietly running a different one would
    be worse than failing.

    Raises :class:`~primer.model.except_.NotFoundError` when the resolved
    profile (or, for an aggregated profile, any of its members) is
    absent, which the REST layer renders as 404 and the executor-build
    path surfaces as a failed turn.
    """
    profile_id = override_profile_id or default_profile_id
    storage = storage_provider.get_storage(ModelProfile)
    row = await storage.get(profile_id)
    if row is None:
        raise NotFoundError(f"ModelProfile {profile_id!r} does not exist")
    return await _flatten(storage, row)


async def resolve_llm(
    storage_provider: StorageProvider,
    provider_registry: "ProviderRegistry",
    *,
    default_profile_id: str,
    override_profile_id: str | None = None,
) -> tuple["LLM", ResolvedModel]:
    """Resolve both the adapter and the model facts for one turn.

    ``kind == "single"`` delegates to ``provider_registry.get_llm(
    profile.provider_id)``. ``kind == "aggregated"`` delegates to
    ``provider_registry.get_aggregated_llm(profile_id, ...)``, which
    returns the SAME :class:`~primer.llm.aggregated.AggregatedLLM`
    instance on every call for a given profile id (cached, keyed by
    profile id, re-fetched by the registry itself INSIDE its lock -- not
    the row this function already fetched above, which is a separate,
    outside-the-lock read; see get_aggregated_llm's own docstring for why
    that distinction is load-bearing) -- NOT a fresh one per call. A
    fresh instance every call would reset ``AggregatedLLM._cursor`` to 0
    each time, so ROUND_ROBIN routing would never actually rotate past
    member[0] (SEQUENTIAL is unaffected -- it always starts at
    members[0] by design). This matches the old ``aggregated``
    LLMProvider's own behaviour, where ``ProviderRegistry._llm_cache``
    made the instance process-wide. ``resolve_member`` recurses by
    member PROFILE id through this same function, rather than by
    provider id through ``provider_registry.get_llm`` directly.
    """
    profile_id = override_profile_id or default_profile_id
    storage = storage_provider.get_storage(ModelProfile)
    row = await storage.get(profile_id)
    if row is None:
        raise NotFoundError(f"ModelProfile {profile_id!r} does not exist")
    resolved = await _flatten(storage, row)
    if row.kind == "single":
        llm = await provider_registry.get_llm(row.provider_id)
        return llm, resolved

    async def _resolve_member(member_id: str) -> tuple["LLM", ResolvedModel]:
        return await resolve_llm(
            storage_provider, provider_registry, default_profile_id=member_id,
        )

    llm = await provider_registry.get_aggregated_llm(
        profile_id, resolve_member=_resolve_member,
    )
    return llm, resolved


__all__ = ["ResolvedModel", "resolve_llm", "resolve_model"]
