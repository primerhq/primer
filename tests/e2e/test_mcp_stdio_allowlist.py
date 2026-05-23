"""E2E test: MCP stdio allowlist enforcement on the /tools call path.

Backlog item:

* T0245 — Create a toolset with provider=mcp / transport=stdio whose
  command[0] is not in ``AppConfig.mcp_stdio_allowed_commands``;
  ``POST /v1/toolsets`` accepts the row (allowlist is enforced lazily
  at session-open, NOT at row create); the subsequent
  ``GET /v1/toolsets/{id}/tools`` must return a clean 503
  ``/errors/service-unavailable`` envelope, never ``/errors/internal``.

  Pins the allowlist short-circuit added in
  [`matrix/toolset/mcp.py`](../../matrix/toolset/mcp.py) (raises
  ``ConfigError`` → mapped to 503 by the registry error mapper),
  and the bringup knob ``mcp_stdio_allowed_commands: [npx, python,
  uv]`` rendered by ``scripts/e2e/bringup.sh``.

  Was DEFERRED for the longest time because the bringup script
  didn't render the allowlist field (so any command was permitted).
  Same iteration that picked T0245 added the bringup knob — both
  changes shipped together.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0245_mcp_stdio_command_outside_allowlist_returns_503(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0245 — POST a toolset whose stdio ``command[0]`` is not in
    the allowlist (the bringup config sets
    ``mcp_stdio_allowed_commands: [npx, python, uv]``). The POST
    succeeds because the allowlist is checked lazily at session-open
    time, NOT at row create. The first call that needs an MCP
    session — ``GET /v1/toolsets/{id}/tools`` — surfaces 503
    ``/errors/service-unavailable`` with a detail string referencing
    the allowlist; the envelope MUST NOT be ``/errors/internal``.

    Defense for ``matrix/toolset/mcp.py`` allowlist short-circuit
    (raises ``ConfigError`` → 503 per ``matrix/api/errors.py``).
    """
    toolset_id = f"ts-t245-{unique_suffix}"

    # Pick a command guaranteed not to be in the allowlist.
    # The bringup-rendered allowlist contains [npx, python, uv];
    # any unique sentinel will do.
    disallowed = f"nope-not-allowed-{unique_suffix}"

    # ---- POST should succeed; allowlist is checked lazily. ----
    r = await client.post(
        "/v1/toolsets",
        json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {
                    "command": [disallowed, "--no-such-flag"],
                    "env": {},
                },
            },
        },
    )
    assert r.status_code == 201, r.text

    try:
        # ---- GET /tools must surface 503 cleanly. ----
        r = await client.get(f"/v1/toolsets/{toolset_id}/tools")
        assert r.status_code == 503, (
            f"GET /v1/toolsets/{toolset_id}/tools returned "
            f"{r.status_code} (expected 503 from allowlist refusal): "
            f"{r.text}"
        )
        body = r.json()

        # RFC 7807 envelope shape.
        assert "type" in body, body
        assert "title" in body, body
        assert "status" in body, body
        assert body["status"] == 503, body

        # Type must not leak the internal-error envelope.
        err_type = body["type"]
        assert "internal" not in err_type, (
            f"503 envelope type leaked /errors/internal: {body!r}"
        )
        # Documented mapping for ConfigError per matrix/api/errors.py.
        assert err_type.endswith("/errors/service-unavailable"), (
            f"Expected /errors/service-unavailable; got type={err_type!r}"
        )

        # Detail must reference the allowlist + the offending command
        # so operators can act on it. The MCP layer raises:
        #   "toolset {id!r}: stdio command {command!r} is not in the
        #    allowlist; set `allowed_stdio_commands` on the registry /
        #    AppConfig to permit it."
        detail = body.get("detail", "") or ""
        assert "allowlist" in detail.lower() or "allowed" in detail.lower(), (
            f"503 detail doesn't mention allowlist: {detail!r}"
        )
        assert disallowed in detail, (
            f"503 detail doesn't echo the rejected command "
            f"{disallowed!r}: {detail!r}"
        )
    finally:
        try:
            await client.delete(f"/v1/toolsets/{toolset_id}")
        except Exception:  # noqa: BLE001
            pass
