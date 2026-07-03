"""Router registration for the FastAPI app.

Extracted verbatim from :mod:`primer.api.app` as part of the app.py
decomposition. ``_mount_routers`` mounts every router under the API
version prefix (subject to the runtime mode), and is consumed by both
``create_app`` and ``create_test_app`` via a re-export from
``primer.api.app``.
"""

from __future__ import annotations

from fastapi import FastAPI

from primer.api.routers import (
    chats as chats_router,
    compute,
    health,
    internal_collections,
    knowledge,
    providers,
    sessions as sessions_router,
    workers as workers_router,
    workspaces as workspaces_router,
    yields as yields_router,
)
from primer.api.routers.auth import auth_router
from primer.api.routers.semantic_search import semantic_search_router
from primer.api.routers.web_fetch import (
    web_fetch_active_config_router,
    web_fetch_providers_helpers_router,
    web_fetch_providers_router,
)
from primer.api.routers.web_search import (
    web_search_active_config_router,
    web_search_providers_helpers_router,
    web_search_providers_router,
)
from primer.api.version import API_VERSION
from primer.model.scheduler import RuntimeMode


def _mount_routers(
    app: FastAPI,
    runtime_mode: RuntimeMode = RuntimeMode.API_PLUS_WORKER,
) -> None:
    """Mount routers under the API version prefix.

    In :class:`RuntimeMode.WORKER` mode only the always-on observability
    surface (``/v1/health`` and ``/v1/workers``) is mounted — entity
    routers (workspaces, sessions, providers, knowledge, internal
    collections, compute) are skipped because the worker process does
    not serve external traffic.
    """
    from fastapi import Depends
    from primer.api.deps import require_auth

    prefix = f"/{API_VERSION}"
    # Always-on routers — health probes + worker observability/drain.
    # Public: no auth dep so liveness/readiness probes work pre-login.
    app.include_router(health.router, prefix=prefix)
    app.include_router(workers_router.router, prefix=prefix)
    # Auth router — mounted unconditionally so login/register work in
    # all runtime modes. The router itself enforces single-user-v1.
    # Public by design (the endpoints ARE the auth surface).
    app.include_router(auth_router, prefix=prefix)
    if runtime_mode == RuntimeMode.WORKER:
        return

    # Everything below requires a valid session cookie. require_auth
    # is applied at include-router time so router internals don't need
    # to change (Commit 6).
    auth_dep = [Depends(require_auth)]

    # API token CRUD — sibling of the auth router under /v1/auth/tokens.
    # Cookie-only by router-internal check; bearer-authenticated callers
    # are rejected so a bearer token can't mint or manage other tokens.
    from primer.api.routers.api_tokens import api_tokens_router
    app.include_router(api_tokens_router, prefix=prefix, dependencies=auth_dep)

    # Phase 1 — providers + tools
    app.include_router(providers.llm_provider_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(providers.embedding_provider_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(providers.cross_encoder_provider_router, prefix=prefix, dependencies=auth_dep)
    # builtin_toolsets_router MUST be registered before toolset_router so
    # GET /toolsets/builtin is matched by the literal route rather than
    # being captured as toolset_id="builtin" by the CRUD GET-by-id.
    app.include_router(providers.builtin_toolsets_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(providers.toolset_router, prefix=prefix, dependencies=auth_dep)
    # Spec B §3.4 — flat tool catalogue for the graph editor's ToolCall
    # picker. Sibling of providers.builtin_toolsets_router's nested
    # ``GET /tools`` (which the operator console's tool/agent pages use);
    # mounted at the disambiguated ``/tools/catalogue`` path.
    from primer.api.routers.tools import tools_router
    app.include_router(tools_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(semantic_search_router, prefix=prefix, dependencies=auth_dep)
    from primer.api.routers.artifact_storage import artifact_storage_router
    app.include_router(artifact_storage_router, prefix=prefix, dependencies=auth_dep)
    # web_search_providers_helpers_router MUST be registered before
    # web_search_providers_router so GET /web_search_providers/_types is
    # matched by the literal route rather than being captured as id="_types"
    # by the CRUD GET-by-id. Same for POST /_test.
    app.include_router(web_search_providers_helpers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_search_providers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_search_active_config_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_fetch_providers_helpers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_fetch_providers_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(web_fetch_active_config_router, prefix=prefix, dependencies=auth_dep)
    # Phase 2 — compute (Agent + Graph)
    app.include_router(compute.agent_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(compute.graph_router, prefix=prefix, dependencies=auth_dep)
    # Phase 3 — knowledge (Collection + Document).
    app.include_router(knowledge.collection_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(knowledge.document_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(internal_collections.router, prefix=prefix, dependencies=auth_dep)
    # Workspaces (providers, templates, workspaces + sub-resources).
    app.include_router(workspaces_router.provider_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.template_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.workspace_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.sessions_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.files_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.log_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(workspaces_router.yields_pending_router, prefix=prefix, dependencies=auth_dep)
    # Workspace events history — bounded backfill for the Studio activity stream.
    app.include_router(workspaces_router.events_router, prefix=prefix, dependencies=auth_dep)
    # Sessions.
    app.include_router(sessions_router.nested_session_router, prefix=prefix, dependencies=auth_dep)
    app.include_router(sessions_router.top_session_router, prefix=prefix, dependencies=auth_dep)
    # Workspace tap — read-only SSE event stream (Spec §3).
    from primer.api.routers.tap import tap_router
    app.include_router(tap_router, prefix=prefix, dependencies=auth_dep)
    # Yields.
    app.include_router(yields_router.yields_router, prefix=prefix, dependencies=auth_dep)
    # Chat REST. NOTE: the WS endpoint inside this router cannot use
    # the same dep mechanism (FastAPI does not enforce auth deps on
    # WebSocket routes the same way). The WS handler reads
    # request.state.user manually after upgrade — see chats.py.
    app.include_router(chats_router.chats_router, prefix=prefix, dependencies=auth_dep)
    # Workspace integrated terminal — bidirectional PTY WebSocket (Studio
    # spec §6.5). Like the chat WS, the handler enforces auth manually via
    # require_auth_ws (the include-time dep does not gate WebSocket routes).
    from primer.api.routers.terminal import terminal_router
    app.include_router(terminal_router, prefix=prefix, dependencies=auth_dep)
    # Tool approval policies.
    from primer.api.routers.tool_approval import make_tool_approval_router
    app.include_router(make_tool_approval_router(), prefix=prefix, dependencies=auth_dep)
    # Channel providers and channels.
    from primer.api.routers.channels import (
        make_channel_provider_router,
        make_channel_router,
    )
    app.include_router(make_channel_provider_router(), prefix=prefix, dependencies=auth_dep)
    app.include_router(make_channel_router(), prefix=prefix, dependencies=auth_dep)
    # Harness REST router.
    from primer.api.routers.harness import harness_router
    app.include_router(harness_router, dependencies=auth_dep)
    # Triggers REST router (Spec §10).
    from primer.api.routers.triggers import triggers_router
    app.include_router(triggers_router, dependencies=auth_dep)
    # Webhook inbound endpoint -- mounted WITHOUT auth so external callers
    # can POST to it. The token in the URL path is the capability credential.
    from primer.api.routers.webhooks import webhooks_router
    app.include_router(webhooks_router)
    # MCP exposure CRUD — Spec §10. Cookie-gated for writes (the
    # router itself rejects bearer-token PUTs); reads pass through.
    from primer.api.routers.mcp_exposure import mcp_exposure_router
    app.include_router(mcp_exposure_router, prefix=prefix, dependencies=auth_dep)
    # Instrumentation endpoints — only mounted when the env var is set.
    # Public to keep the distributed test harness simple; the env var
    # itself is the access gate.
    import os as _os
    if _os.environ.get("PRIMER_ENABLE_TEST_ENDPOINTS") == "1":
        from primer.api.routers._test_endpoints import router as _test_router
        app.include_router(_test_router, prefix=prefix)
