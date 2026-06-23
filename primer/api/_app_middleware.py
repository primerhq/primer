"""Middleware, static-asset mounts, and route-installer helpers.

Extracted verbatim from :mod:`primer.api.app` as part of the app.py
decomposition. Holds the gzip middleware, security/CSP/request-id
middleware installers, the operator-console JSX bundle + static mount,
the Prometheus metrics mount, the root redirect, and the cookie-auth
middleware installer. Both ``create_app`` and ``create_test_app``
consume these via re-exports from ``primer.api.app``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re as _re
import uuid as _uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from primer.api._jsx_bundle import build_jsx_bundle
from primer.api.config import AppConfig


logger = logging.getLogger(__name__)


class _GZipExceptMcp(GZipMiddleware):
    """Bypass gzip for paths under ``/v1/mcp``.

    The global :class:`GZipMiddleware` buffers + compresses response
    bodies, which breaks the chunked SSE stream the MCP
    StreamableHTTP transport relies on (no flush boundaries; the
    client never sees an event until the body completes). Other
    endpoints continue to benefit from compression unchanged.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/v1/mcp"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _mount_metrics(app: FastAPI, config: AppConfig) -> None:
    """Mount the Prometheus ``/metrics`` endpoint when metrics are enabled.

    The endpoint is mounted via :func:`prometheus_client.make_asgi_app`
    which returns a bare ASGI application wrapping the Primer-specific
    :data:`primer.observability.metrics.registry`.  Mounting happens
    *before* error handlers so the mount does not go through FastAPI's
    exception machinery.

    When ``config.observability.metrics_enabled`` is *False* or
    ``config.observability.enabled`` is *False* the mount is skipped
    entirely and ``GET /metrics`` returns a 404.
    """
    if not config.observability.enabled or not config.observability.metrics_enabled:
        return

    from prometheus_client import make_asgi_app as _make_metrics_asgi
    from primer.observability.metrics import registry as _metrics_registry

    metrics_app = _make_metrics_asgi(registry=_metrics_registry)
    app.mount("/metrics", metrics_app)


def _install_root_redirect(app: FastAPI) -> None:
    """GET / -> 307 redirect to /console/.

    Operators land at the host root expecting the console; without this
    they get a bare 404 from FastAPI. The console mount handles its own
    trailing-slash redirect from /console -> /console/.
    """
    from starlette.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    async def _root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/console/", status_code=307)


def _install_auth_middleware(app: FastAPI) -> None:
    """Install the cookie-auth middleware.

    Populates ``request.state.user`` / ``.principal`` from a signed
    ``primer_session`` cookie. Does not itself 401; routers do that
    via :func:`primer.api.deps.require_auth`.
    """
    from primer.api.middleware.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware)


def _install_security_headers(app: FastAPI) -> None:
    """Set conservative defensive headers on every response.

    The API is JSON-only, so a strict ``no-sniff`` + deny-frame policy
    is safe by default. ``Cross-Origin-Resource-Policy: same-origin``
    blocks no-CORS embeds from other origins. CSP for the JSON surface
    is handled by ``_install_console_csp`` which scopes the policy to
    the ``/console/*`` mount only — JSON responses never carry one.
    """
    @app.middleware("http")
    async def _security_headers(request, call_next):  # noqa: ARG001
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin",
        )
        response.headers.setdefault(
            "Cross-Origin-Resource-Policy", "same-origin",
        )
        return response


# CSP header for the /console/* static mount.
#
# Why 'unsafe-eval' AND 'unsafe-inline' are both required:
# @babel/standalone has two paths for running <script type="text/babel">
# tags. (1) ``new Function`` over the transpiled body — covered by
# 'unsafe-eval'. (2) When the source comes from a `src=` attribute it
# fetches the file, transpiles it, and injects a fresh <script> element
# whose body is the transpiled code INLINE — that's an inline script
# and CSP blocks it without 'unsafe-inline'. The .jsx files are all
# `src`-loaded, so path (2) is what we hit; the console page renders
# blank without 'unsafe-inline'.
#
# Why there are NO `sha384-...` entries in script-src:
# CSP hash source-list entries (`'sha-*'`) allow inline script BLOCKS
# whose content hashes to a listed value — they are NOT a way to pin
# external script integrity. External-script integrity is enforced by
# the `integrity="sha384-..."` attribute on the `<script src=...>` tag
# (Subresource Integrity, a separate browser layer). More importantly,
# per CSP spec the presence of ANY hash/nonce in script-src causes
# 'unsafe-inline' to be silently ignored — defeating the inline-script
# allowance we need for Babel path (2). The CDN script integrity is
# preserved unchanged by the `integrity=` attributes already on the
# script tags in `ui/index.html`.
#
# Trust chain after this CSP:
#   1. CDN scripts (React, ReactDOM, Babel-standalone) load only from
#      `https://unpkg.com` and are verified by SRI on the script tag.
#   2. .jsx files load only from `'self'`.
#   3. `connect-src 'self'` blocks all exfiltration to other origins.
#   4. The XSS path 'unsafe-inline' normally opens — injected inline
#      <script> in served HTML — has no entry point here: nothing
#      user-controlled lands in /console/* content. An attacker would
#      need write access to ui/ directly, at which point CSP is moot.
#   5. The alternative — pre-compile the JSX at build time — requires
#      an npm-installed Babel CLI, which the project forbids on the
#      host (Shai-Hulud mitigation).
# Documented in docs/superpowers/specs/2026-05-15-web-console-implementation-design.md §2.2.
_CONSOLE_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-eval' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


def _install_console_csp(app: FastAPI) -> None:
    """Apply a strict CSP only to ``/console/*`` responses.

    JSON responses on ``/v1/*`` are not browser-renderable so CSP has no
    effect on them. Scoping the policy to the static UI mount keeps the
    JSON surface unchanged and avoids any unintended interaction with
    OpenAPI / Swagger / ReDoc when log_level=debug is set.
    """
    @app.middleware("http")
    async def _console_csp(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/console"):
            # Direct assignment, not setdefault — the policy is strict
            # by intent; no downstream handler should be loosening it.
            response.headers["Content-Security-Policy"] = _CONSOLE_CSP
            # Cache-Control is set per-file inside _CachingStaticFiles
            # (immutable for ui/vendor/*, no-cache for index.html,
            # short-lived public for everything else). Don't blanket
            # it here or the StaticFiles values get clobbered.
        return response


# Directory containing the operator console (the bind-mounted ui/
# folder at repo root). Computed once at import time; the production
# factory guards on .is_dir() so a deployment that strips the directory
# still boots without the console mount.
_UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"


# Request-id propagation. Honour an incoming X-Request-Id when it
# parses as a safe token; otherwise mint a fresh one. Stashed on
# request.state.request_id so error handlers can embed it into the
# RFC 7807 envelope (extensions.request_id) and the UI's "Copy
# request id" action has something to surface.
#
# Defensive guard on the incoming value: cap length + restrict to a
# conservative character set so a malicious client cannot smuggle
# control characters / log-injection payloads through the header
# (the value is echoed on the response and logged structurally).
_VALID_REQUEST_ID = _re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _install_request_id(app: FastAPI) -> None:
    """Stamp X-Request-Id on every response; expose it via request.state.

    Incoming X-Request-Id values are honoured when they match the
    conservative regex above; otherwise a fresh ``req-<uuid hex[:12]>``
    is generated. The id is set on the response header and stashed at
    ``request.state.request_id`` for downstream consumers (the error
    mapper threads it into ``extensions.request_id``).
    """
    @app.middleware("http")
    async def _request_id(request, call_next):
        incoming = request.headers.get("X-Request-Id")
        if incoming and _VALID_REQUEST_ID.match(incoming):
            rid = incoming
        else:
            rid = "req-" + _uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


def _install_jsx_bundle(app: FastAPI, *, docs_url: str = "") -> None:
    """Precompile every text/babel script at startup, register a route
    that serves the concatenated bundle at ``/console/_app.js``.

    Why a Route instead of writing the bundle to disk: keeps the
    repo's ``ui/`` tree clean (no build artefacts), and the in-memory
    body is what every subsequent request reads anyway.

    Cache strategy: short max-age + strong ETag, so reloads after a
    backend redeploy revalidate quickly (304 when nothing changed,
    fresh bytes when bundle hash flipped) without needing the URL
    to embed the hash.

    Server config surfaced to the browser: the console is served as a
    static ``index.html`` (no template seam), so server-side flags reach
    the page by being prepended to this server-built bundle as
    ``window.__PRIMER_*__`` globals. ``docs_url`` rides this seam so the
    console's external "Docs" link can read it.
    """
    from starlette.responses import Response

    etag, body = build_jsx_bundle(_UI_DIR)
    if body and docs_url:
        # Prepend the server-config preamble so the global is defined
        # before any console script runs, then re-derive the ETag so a
        # docs_url change invalidates caches.
        preamble = (
            "window.__PRIMER_DOCS_URL__ = "
            + json.dumps(docs_url)
            + ";\n"
        ).encode("utf-8")
        body = preamble + body
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
    if not body:
        # No UI dir or no Babel — leave route unregistered; the
        # console will 404 on /_app.js and the static mount handles
        # the rest as before.
        return

    @app.get("/console/_app.js", include_in_schema=False)
    async def _serve_jsx_bundle(request: Request) -> Response:
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={
                "ETag": etag,
                "Cache-Control": "public, max-age=300, must-revalidate",
            })
        return Response(
            content=body,
            media_type="application/javascript",
            headers={
                "ETag": etag,
                "Cache-Control": "public, max-age=300, must-revalidate",
            },
        )


class _CachingStaticFiles(StaticFiles):
    """StaticFiles + path-aware Cache-Control.

    Caching strategy:

    * ``index.html``  → ``no-cache`` so any deploy is picked up on
      next navigation. Sub-resources it references are still subject
      to their own per-file policy below.
    * ``vendor/*``    → ``public, max-age=1y, immutable``. These are
      pinned third-party builds (see ui/vendor/MANIFEST.md); when we
      bump a version the filename will change anyway.
    * everything else → ``public, max-age=300, must-revalidate``.
      Short enough that an edited .jsx/.css shows up in the browser
      within five minutes without a hard refresh, long enough that
      asset-heavy panels don't hit the network on every navigation.

    Starlette's StaticFiles already emits Last-Modified, so the
    must-revalidate path is a cheap 304 round-trip rather than a
    full re-download.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code != 200:
            return response
        # Starlette normalises bare ``/console/`` to ``"."`` via
        # os.path.normpath(""), and html=True maps that to
        # ``index.html`` internally — so cover both spellings.
        if path in ("", ".", "index.html"):
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("vendor/") or path.startswith("vendor" + os.sep):
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        else:
            response.headers["Cache-Control"] = (
                "public, max-age=300, must-revalidate"
            )
        return response


def _mount_console(app: FastAPI) -> None:
    """Mount the operator console at ``/console`` if the ui/ dir is present.

    ``html=True`` makes StaticFiles serve ``index.html`` for the bare
    ``/console/`` prefix. Only invoked from :func:`create_app` (the
    production factory); tests intentionally do not get the static
    mount.
    """
    if _UI_DIR.is_dir():
        app.mount(
            "/console",
            _CachingStaticFiles(directory=str(_UI_DIR), html=True),
            name="console",
        )
    else:
        logger.info(
            "ui/ directory not found at %s; /console mount skipped",
            _UI_DIR,
        )
