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
    from primer.api.deps import require_admin, require_user

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

    # RBAC role gates (§6.2), applied at include-router time:
    #   admin_dep -> require_admin (role == "admin")            provider/
    #                                                           system config
    #   user_dep  -> require_user  (role in {"user","admin"})   feature routers
    # Restricted users (role == "restricted") reach NO entity router.
    # WebSocket routes are NOT gated here: FastAPI injects request=None for
    # the WS scope and require_user/require_admin short-circuit to None for
    # it, so the WS handlers enforce roles themselves (see Task 8).
    admin_dep = [Depends(require_admin)]
    user_dep = [Depends(require_user)]

    # API token CRUD — sibling of the auth router under /v1/auth/tokens.
    # Cookie-only by router-internal check; bearer-authenticated callers
    # are rejected so a bearer token can't mint or manage other tokens.
    # Personal per-user resource => require_user: any user/admin manages
    # their OWN tokens; restricted users are blocked from minting.
    from primer.api.routers.api_tokens import api_tokens_router
    app.include_router(api_tokens_router, prefix=prefix, dependencies=user_dep)

    # Phase 1 — providers (system configuration => admin only).
    app.include_router(providers.llm_provider_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(providers.embedding_provider_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(providers.cross_encoder_provider_router, prefix=prefix, dependencies=admin_dep)
    # Toolsets are an authoring feature (agents/graphs reference them) =>
    # require_user. builtin_toolsets_router MUST be registered before
    # toolset_router so GET /toolsets/builtin is matched by the literal
    # route rather than being captured as toolset_id="builtin".
    app.include_router(providers.builtin_toolsets_router, prefix=prefix, dependencies=user_dep)
    app.include_router(providers.toolset_router, prefix=prefix, dependencies=user_dep)
    # Spec B §3.4 — flat tool catalogue for the graph editor's ToolCall
    # picker => feature => require_user.
    from primer.api.routers.tools import tools_router
    app.include_router(tools_router, prefix=prefix, dependencies=user_dep)
    # SemanticSearchProvider CRUD — a provider => admin only.
    app.include_router(semantic_search_router, prefix=prefix, dependencies=admin_dep)
    from primer.api.routers.artifact_storage import artifact_storage_router
    app.include_router(artifact_storage_router, prefix=prefix, dependencies=admin_dep)
    # web_search / web_fetch providers — system configuration => admin.
    # helpers_router MUST be registered before providers_router so the
    # literal /_types + /_test routes win over the CRUD GET-by-id.
    app.include_router(web_search_providers_helpers_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(web_search_providers_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(web_search_active_config_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(web_fetch_providers_helpers_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(web_fetch_providers_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(web_fetch_active_config_router, prefix=prefix, dependencies=admin_dep)
    # Phase 2 — compute (Agent + Graph) — authoring feature => require_user.
    app.include_router(compute.agent_router, prefix=prefix, dependencies=user_dep)
    app.include_router(compute.graph_router, prefix=prefix, dependencies=user_dep)
    # Phase 3 — knowledge (Collection + Document) — feature => require_user.
    app.include_router(knowledge.collection_router, prefix=prefix, dependencies=user_dep)
    app.include_router(knowledge.document_router, prefix=prefix, dependencies=user_dep)
    # Internal (system) collections config/bootstrap — admin only.
    app.include_router(internal_collections.router, prefix=prefix, dependencies=admin_dep)
    # Workspaces. NAME-COLLISION: the workspace PROVIDER CRUD is system
    # configuration => admin; the workspace FEATURE (workspaces + their
    # session/files/log/yields/events sub-resources) is => require_user.
    app.include_router(workspaces_router.provider_router, prefix=prefix, dependencies=admin_dep)
    app.include_router(workspaces_router.template_router, prefix=prefix, dependencies=user_dep)
    app.include_router(workspaces_router.workspace_router, prefix=prefix, dependencies=user_dep)
    app.include_router(workspaces_router.sessions_router, prefix=prefix, dependencies=user_dep)
    app.include_router(workspaces_router.files_router, prefix=prefix, dependencies=user_dep)
    app.include_router(workspaces_router.log_router, prefix=prefix, dependencies=user_dep)
    app.include_router(workspaces_router.yields_pending_router, prefix=prefix, dependencies=user_dep)
    # Workspace events history — bounded backfill for the Studio activity
    # stream; a workspace sub-resource like files/log/yields => require_user.
    # (Not in the original §6.2 table — added after Task 7's brief was
    # written; classified by analogy with its sibling sub-resource routers.)
    app.include_router(workspaces_router.events_router, prefix=prefix, dependencies=user_dep)
    # Sessions — feature => require_user.
    app.include_router(sessions_router.nested_session_router, prefix=prefix, dependencies=user_dep)
    app.include_router(sessions_router.top_session_router, prefix=prefix, dependencies=user_dep)
    # Workspace tap — read-only SSE (workspace feature) => require_user.
    # (WS role gate lives in the handler — Task 8.)
    from primer.api.routers.tap import tap_router
    app.include_router(tap_router, prefix=prefix, dependencies=user_dep)
    # Yields — session/workspace feature => require_user.
    app.include_router(yields_router.yields_router, prefix=prefix, dependencies=user_dep)
    # Chat REST + WS — feature => require_user. NOTE: the WS endpoint
    # inside this router cannot use the include-time dep (FastAPI does not
    # enforce deps on WebSocket routes the same way); the WS handler
    # enforces its own role gate via require_auth_ws + accept-then-close
    # 4403 — see chats.py / Task 8.
    app.include_router(chats_router.chats_router, prefix=prefix, dependencies=user_dep)
    # Workspace integrated terminal — bidirectional PTY WebSocket (Studio
    # spec §6.5). Feature => require_user at include time; the WS handler
    # additionally admin-OR-explicit-enable gates (Task 8).
    from primer.api.routers.terminal import terminal_router
    app.include_router(terminal_router, prefix=prefix, dependencies=user_dep)
    # Tool approval policies — system policy => admin only.
    from primer.api.routers.tool_approval import make_tool_approval_router
    app.include_router(make_tool_approval_router(), prefix=prefix, dependencies=admin_dep)
    # Channels. NAME-COLLISION: the channel PROVIDER CRUD is system
    # configuration => admin; channels + bindings (the feature) is
    # => require_user.
    from primer.api.routers.channels import (
        make_channel_provider_router,
        make_channel_router,
    )
    app.include_router(make_channel_provider_router(), prefix=prefix, dependencies=admin_dep)
    app.include_router(make_channel_router(), prefix=prefix, dependencies=user_dep)
    # Harness REST router — feature => require_user (no prefix; the router
    # carries its own version segment).
    from primer.api.routers.harness import harness_router
    app.include_router(harness_router, dependencies=user_dep)
    # Triggers REST router (Spec §10) — feature => require_user (no prefix).
    from primer.api.routers.triggers import triggers_router
    app.include_router(triggers_router, dependencies=user_dep)
    # Webhook inbound endpoint -- mounted WITHOUT auth so external callers
    # can POST to it. The token in the URL path is the capability credential.
    from primer.api.routers.webhooks import webhooks_router
    app.include_router(webhooks_router)
    # MCP exposure CRUD — Spec §10. Admin only (§6.2). Task 9 additionally
    # hardens the PUT handler with an in-router require_admin (defense in
    # depth for the bearer/MCP dispatch path); this include-time gate is
    # the primary control.
    from primer.api.routers.mcp_exposure import mcp_exposure_router
    app.include_router(mcp_exposure_router, prefix=prefix, dependencies=admin_dep)
    # Admin users CRUD — operator-only account management (RBAC). Mounted
    # at /v1/admin/users and gated by require_admin (stricter than the
    # require_auth applied to the routers above).
    from primer.api.routers.admin_users import admin_users_router
    from primer.api.deps import require_admin
    app.include_router(
        admin_users_router, prefix=prefix, dependencies=[Depends(require_admin)],
    )
    # Admin API-key management — view/revoke any user's tokens. Admin-only,
    # nested under the user for the console drill-down.
    from primer.api.routers.admin_tokens import admin_tokens_router
    app.include_router(
        admin_tokens_router, prefix=prefix, dependencies=[Depends(require_admin)],
    )
    # OIDC SSO providers CRUD — system configuration => admin only.
    # client_secret (SecretStr) is auto-masked by pydantic's default dump.
    from primer.api.routers.oidc_providers import oidc_providers_router
    app.include_router(
        oidc_providers_router, prefix=prefix, dependencies=[Depends(require_admin)],
    )
    # Instrumentation endpoints — only mounted when the env var is set.
    # Public to keep the distributed test harness simple; the env var
    # itself is the access gate.
    import os as _os
    if _os.environ.get("PRIMER_ENABLE_TEST_ENDPOINTS") == "1":
        from primer.api.routers._test_endpoints import router as _test_router
        app.include_router(_test_router, prefix=prefix)
