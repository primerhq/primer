"""API version identifier shared across routers and OpenAPI metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

API_VERSION = "v1"
"""URL-prefix segment for every router in this API.

Mounted as ``/{API_VERSION}/<resource>`` (e.g. ``/v1/health``). Bumping
this is a backwards-incompatible change; future versions will be
introduced as additional routers (``/v2/...``) sharing the same
process.
"""

try:
    APP_VERSION = version("primer-ai")
except PackageNotFoundError:  # source tree with no installed distribution
    APP_VERSION = "0.0.0+dev"
"""Semver of the running build, surfaced in OpenAPI ``info.version`` and the
``GET /v1/health`` payload. Read from the installed ``primer-ai`` distribution
metadata (which python-semantic-release bumps at release time) so it can never
drift from the published version — a hand-maintained constant here silently
froze at 0.1.0 through the 0.2.0 release. Falls back to ``0.0.0+dev`` when
imported from a source checkout with no installed distribution."""

__all__ = ["API_VERSION", "APP_VERSION"]
