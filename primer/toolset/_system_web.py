"""The ``system`` tools that read and set where web searches and page fetches go.

Four tools, the twins of ``GET`` / ``PUT /v1/web_search_active_config`` and ``/v1/web_fetch_active_config``:

* ``get_web_search_active_config`` / ``get_web_fetch_active_config`` return the stored ``config`` and ``providers``: the first 200
  provider rows by id, as ``{id, provider_type}``, which are the ids a set may name;
* ``set_web_search_active_config`` / ``set_web_fetch_active_config`` replace the stored ``config``. They run the PUT routes' own
  check and write (``primer/web_search/active_config.py``, ``primer/web_fetch/active_config.py``), so a set refuses what the route
  refuses, in the route's words, and stores nothing then.

``required_role="admin"``, as the routes (``primer/api/_app_routes.py`` mounts them behind the admin dependency). A set is one row
write, so it keeps the ``make_tool`` default and a Stop may cancel it.

Unlike the routes, a set does not drop the services' active-config cache. That cache lives in each process and a tool may run in
another one (a worker in the k3s split), so every process picks the new choice up when its copy expires, within the services'
``cache_ttl_seconds`` (5 s, ``WebSearchService`` / ``WebFetchService``). The set descriptions say so.

Storages are resolved inside the handlers, not when the toolset is built: tests and the docs checks build the toolset over fakes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import PrimerError
from primer.model.storage import OffsetPage, OrderBy
from primer.model.web_fetch import (
    ACTIVE_WEB_FETCH_CONFIG_ID,
    ActiveFetchConfig,
    ActiveWebFetchConfig,
    WebFetchProvider,
)
from primer.model.web_search import (
    ACTIVE_WEB_SEARCH_CONFIG_ID,
    ActiveProviderConfig,
    ActiveWebSearchConfig,
    WebSearchProvider,
)
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok as _ok
from primer.toolset._system_common import SYSTEM_TOOLSET_ID, _err_from_primer, _err_from_validation
from primer.web_fetch import active_config as _fetch_config
from primer.web_search import active_config as _search_config

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider
    from primer.toolset.internal import ToolHandler


# The first page of provider rows a get lists: the API's page cap, which is also what the console asks for.
_PROVIDER_PAGE = 200


class _GetActiveConfigArgs(BaseModel):
    """No arguments."""


class _SetWebSearchActiveConfigArgs(BaseModel):
    config: ActiveProviderConfig = Field(
        ...,
        description=(
            'Where web searches go: {"mode": "single", "provider_id": "<id>"}, or {"mode": "aggregated", "provider_ids": '
            '["<id>", ...]} tried in that order. Every id must be an existing web search provider.'
        ),
    )


class _SetWebFetchActiveConfigArgs(BaseModel):
    config: ActiveFetchConfig = Field(
        ...,
        description=(
            'Where page fetches go: {"mode": "single", "provider_id": "<id>"}, or {"mode": "aggregated", "provider_ids": '
            '["<id>", ...]} tried in that order. Every id must be an existing fetcher.'
        ),
    )


@dataclass(frozen=True)
class _Setting:
    """What differs between the two settings: the words, the models, and the check and write the PUT route runs."""

    stem: str  # the tool ids are get_<stem>_active_config and set_<stem>_active_config
    setting: str  # "web search": the unavailable message
    get_purpose: str
    get_when: str
    get_returns: str
    set_purpose: str
    set_when: str
    set_example: ToolExample
    set_args: type[BaseModel]
    provider_model: type[BaseModel]
    active_model: type[BaseModel]
    active_id: str
    unknown_provider_ids: Callable[..., Awaitable[list[str]]]
    unknown_ids_message: Callable[[list[str]], str]
    save_active_config: Callable[..., Awaitable[Any]]


def _get_when(*, setting: str, set_tool: str, listed: str, agent_tool: str) -> str:
    return (
        f"Use when you need the deployment's current {setting} choice, and before ``{set_tool}``: it returns the stored ``config`` "
        "(``{mode: single, provider_id}``, or ``{mode: aggregated, provider_ids}`` tried in that order) and ``providers``, "
        f"the {listed} that exist (``id`` and ``provider_type``, the first 200 by id), which are the ids the set tool may name. "
        f"One choice per deployment: it answers every agent's ``{agent_tool}`` calls. Admin role. ``type=unavailable`` means the "
        "setting is missing (the server creates it at startup; check the server logs)."
    )


def _set_when(*, noun: str, agent_tool: str, existing: str, get_tool: str) -> str:
    return (
        f"Use when you are asked to change which {noun} answers ``{agent_tool}`` calls. Send ``config`` as "
        "``{mode: single, provider_id}`` or ``{mode: aggregated, provider_ids}`` (tried in that order; a repeated id is kept once). "
        f"Every id must be an existing {existing}: read them with ``{get_tool}``. An unknown id or an empty list returns "
        "``type=validation-error`` and stores nothing. Admin role. Returns the stored ``config``. It takes effect in every server "
        "process within 5 seconds: each process caches the setting for up to 5 seconds and this tool does not clear that cache."
    )


_SEARCH = _Setting(
    stem="web_search",
    setting="web search",
    get_purpose="Read where web searches go: one provider (single mode) or an ordered list tried in turn (aggregated mode).",
    get_when=_get_when(
        setting="web search", set_tool="set_web_search_active_config", listed="web search providers", agent_tool="web_search",
    ),
    get_returns='``{config: {mode: "single", provider_id: "DuckDuckGo"}, providers: [{id: "DuckDuckGo", provider_type: "duckduckgo"}]}``',
    set_purpose="Choose where web searches go: one provider, or an ordered list where the next provider is tried when one fails.",
    set_when=_set_when(
        noun="provider", agent_tool="web_search", existing="web search provider", get_tool="get_web_search_active_config",
    ),
    set_example=ToolExample(
        args={"config": {"mode": "aggregated", "provider_ids": ["tavily-1", "DuckDuckGo"]}},
        returns="``{config: <the stored config>}``",
        note="tavily-1 first, DuckDuckGo when it fails",
    ),
    set_args=_SetWebSearchActiveConfigArgs,
    provider_model=WebSearchProvider,
    active_model=ActiveWebSearchConfig,
    active_id=ACTIVE_WEB_SEARCH_CONFIG_ID,
    unknown_provider_ids=_search_config.unknown_provider_ids,
    unknown_ids_message=_search_config.unknown_ids_message,
    save_active_config=_search_config.save_active_config,
)

_FETCH = _Setting(
    stem="web_fetch",
    setting="web fetch",
    get_purpose="Read where page fetches go: one fetcher (single mode) or an ordered list tried in turn (aggregated mode).",
    get_when=_get_when(setting="web fetch", set_tool="set_web_fetch_active_config", listed="fetchers", agent_tool="web_fetch"),
    get_returns='``{config: {mode: "single", provider_id: "local"}, providers: [{id: "local", provider_type: "local"}]}``',
    set_purpose=(
        "Choose where page fetches go: one fetcher, or an ordered list where the next fetcher is tried when one fails or returns "
        "too little text."
    ),
    set_when=_set_when(noun="fetcher", agent_tool="web_fetch", existing="fetcher", get_tool="get_web_fetch_active_config"),
    set_example=ToolExample(
        args={"config": {"mode": "single", "provider_id": "local"}},
        returns='``{config: {mode: "single", provider_id: "local"}}``',
    ),
    set_args=_SetWebFetchActiveConfigArgs,
    provider_model=WebFetchProvider,
    active_model=ActiveWebFetchConfig,
    active_id=ACTIVE_WEB_FETCH_CONFIG_ID,
    unknown_provider_ids=_fetch_config.unknown_provider_ids,
    unknown_ids_message=_fetch_config.unknown_ids_message,
    save_active_config=_fetch_config.save_active_config,
)


def _get_handler(spec: _Setting, storage_provider: StorageProvider) -> ToolHandler:
    async def handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            row = await storage_provider.get_storage(spec.active_model).get(spec.active_id)
            if row is None:
                return _err(
                    f"the active {spec.setting} config is missing; the server creates it at startup, so check the server logs for "
                    "a bootstrap failure",
                    error_type="unavailable",
                )
            page = await storage_provider.get_storage(spec.provider_model).list(
                OffsetPage(offset=0, length=_PROVIDER_PAGE), order_by=[OrderBy(field="id", direction="asc")],
            )
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok({
            "config": row.config.model_dump(mode="json"),
            "providers": [{"id": p.id, "provider_type": p.provider_type.value} for p in page.items],
        })

    return handler


def _set_handler(spec: _Setting, storage_provider: StorageProvider) -> ToolHandler:
    async def handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = spec.set_args.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        try:
            unknown = await spec.unknown_provider_ids(args.config, storage_provider.get_storage(spec.provider_model))
            if unknown:
                return _err(spec.unknown_ids_message(unknown), error_type="validation-error")
            row = await spec.save_active_config(args.config, storage_provider.get_storage(spec.active_model))
        except PrimerError as exc:
            return _err_from_primer(exc, error_type="storage-error")
        return _ok({"config": row.config.model_dump(mode="json")})

    return handler


def web_active_config_tools(*, storage_provider: StorageProvider) -> dict[str, tuple[Tool, ToolHandler]]:
    """The four tools, keyed by id, for ``build_system_toolset`` to add to the ``system`` registry."""
    out: dict[str, tuple[Tool, ToolHandler]] = {}
    for spec in (_SEARCH, _FETCH):
        get_id, set_id = f"get_{spec.stem}_active_config", f"set_{spec.stem}_active_config"
        out[get_id] = (
            make_tool(
                id=get_id,
                toolset_id=SYSTEM_TOOLSET_ID,
                purpose=spec.get_purpose,
                when=spec.get_when,
                args_schema=_GetActiveConfigArgs.model_json_schema(),
                examples=[ToolExample(args={}, returns=spec.get_returns)],
                required_role="admin",
            ),
            _get_handler(spec, storage_provider),
        )
        out[set_id] = (
            make_tool(
                id=set_id,
                toolset_id=SYSTEM_TOOLSET_ID,
                purpose=spec.set_purpose,
                when=spec.set_when,
                args_schema=spec.set_args.model_json_schema(),
                examples=[spec.set_example],
                required_role="admin",
                preview_args=("config",),
            ),
            _set_handler(spec, storage_provider),
        )
    return out


__all__ = ["web_active_config_tools"]
