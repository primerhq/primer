"""API version identifier shared across routers and OpenAPI metadata."""

from __future__ import annotations

API_VERSION = "v1"
"""URL-prefix segment for every router in this API.

Mounted as ``/{API_VERSION}/<resource>`` (e.g. ``/v1/health``). Bumping
this is a backwards-incompatible change; future versions will be
introduced as additional routers (``/v2/...``) sharing the same
process.
"""

APP_VERSION = "0.1.0"
"""Semver of the API surface itself, surfaced in OpenAPI ``info.version``
and the ``GET /v1/health`` payload. Bump on backwards-compatible
additions; reset minor on breaking changes."""

__all__ = ["API_VERSION", "APP_VERSION"]
