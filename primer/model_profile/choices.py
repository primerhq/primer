"""The model profiles a signed-in user may see: id, description, kind and context length, never the provider or the config (audit G3-02).

``GET /v1/model_profiles`` is admin-only, because a profile is provider configuration: it names an LLM provider, the provider-side
model name and the ``config`` (``reasoning``, ``extended``) applied to every call. Any signed-in user may create or edit an agent
(``POST /v1/agents`` is user tier), and an agent must name a profile, so the console's agent form and a user-role agent that writes
agents need the ids. ``GET /v1/model_profiles/choices`` and the ``system::list_model_profile_choices`` tool answer from here with a
:class:`~primer.model.model_profile.ModelProfileChoice` per profile, sorted by id and paged like every other list (``limit`` with
``offset`` or ``cursor``). Both read the rows through :func:`list_model_profile_choices`, so the route and the tool cannot disagree on
what a user sees.
"""

from __future__ import annotations

from typing import Any

from primer.model.model_profile import ModelProfile, ModelProfileChoice
from primer.model.storage import CursorPage, CursorPageResponse, OffsetPage, OffsetPageResponse, OrderBy

# The one order: by id. A user picks a profile by its id, and a cursor carries the keys its own request sorts by.
_ORDER = [OrderBy(field="id", direction="asc")]


def model_profile_choice(row: ModelProfile) -> ModelProfileChoice:
    """The choice a profile row is listed as: its id, description, kind and stored context length."""
    return ModelProfileChoice(id=row.id, description=row.description, kind=row.kind, context_length=row.context_length)


async def list_model_profile_choices(
    storage: Any, page: OffsetPage | CursorPage,
) -> OffsetPageResponse[ModelProfileChoice] | CursorPageResponse[ModelProfileChoice]:
    """One page of the profiles in ``storage``, sorted by id, each as a :class:`ModelProfileChoice`.

    The page answers in the shape of the request: ``{offset, length, total, items}`` for an offset page and ``{next_cursor, items}``
    for a cursor page, as ``GET /v1/model_profiles`` does.
    """
    resp = await storage.list(page, order_by=_ORDER)
    items = [model_profile_choice(row) for row in resp.items]
    if isinstance(resp, CursorPageResponse):
        return CursorPageResponse[ModelProfileChoice](next_cursor=resp.next_cursor, items=items)
    return OffsetPageResponse[ModelProfileChoice](offset=resp.offset, length=resp.length, total=resp.total, items=items)


__all__ = ["list_model_profile_choices", "model_profile_choice"]
