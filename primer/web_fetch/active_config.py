"""The web fetch active-config check and write, shared by the REST PUT route and the system tools.

``PUT /v1/web_fetch_active_config`` (``primer/api/routers/web_fetch.py``) and the ``set_web_fetch_active_config`` tool
(``primer/toolset/_system_web.py``) store the same singleton row. Both run :func:`unknown_provider_ids` before the write and
:func:`save_active_config` to write it, so a tool cannot store what the route refuses, and the refusal reads the same
(:func:`unknown_ids_message`).

Neither function touches the ``WebFetchService`` active-config cache. The route drops it after a write; a tool does not (the cache is
local to one process and a tool may run in another), so the new choice reaches every process within the cache's 5 second TTL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID,
    ActiveFetchConfig,
    ActiveWebFetchConfig,
    AggregatedFetchConfig,
    SingleFetchConfig,
    WebFetchProvider,
)

if TYPE_CHECKING:
    from primer.int.storage import Storage


async def unknown_provider_ids(config: ActiveFetchConfig, providers: "Storage[WebFetchProvider]") -> list[str]:
    """The ids ``config`` names that have no provider row, in the config's order."""
    if isinstance(config, SingleFetchConfig):
        ids = [config.provider_id]
    elif isinstance(config, AggregatedFetchConfig):
        ids = list(config.provider_ids)
    else:
        ids = []
    unknown: list[str] = []
    for pid in ids:
        if await providers.get(pid) is None:
            unknown.append(pid)
    return unknown


def unknown_ids_message(unknown: list[str]) -> str:
    """The refusal both callers give for ``unknown`` (the REST route's 422 ``message``, the tool's ``validation-error``)."""
    return f"active config references unknown provider id(s): {unknown}"


async def save_active_config(config: ActiveFetchConfig, active: "Storage[ActiveWebFetchConfig]") -> ActiveWebFetchConfig:
    """Write ``config`` as the singleton row: create it when it is missing, else replace it. Returns the stored row."""
    row = ActiveWebFetchConfig(id=ACTIVE_WEB_FETCH_CONFIG_ID, config=config)
    if await active.get(ACTIVE_WEB_FETCH_CONFIG_ID) is None:
        return await active.create(row)
    return await active.update(row)


__all__ = ["save_active_config", "unknown_ids_message", "unknown_provider_ids"]
