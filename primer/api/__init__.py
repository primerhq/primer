"""FastAPI REST API for the primer framework.

Public surface:

* :func:`create_app` — production factory.
* :func:`create_test_app` — test factory.
* :class:`AppConfig` — env-var-driven configuration (DB params only;
  everything else lives in storage).

See ``docs/superpowers/specs/2026-05-08-rest-api-foundation-design.md``
for the surrounding design.
"""

from primer.api.app import create_app, create_test_app
from primer.api.config import AppConfig


__all__ = ["AppConfig", "create_app", "create_test_app"]
