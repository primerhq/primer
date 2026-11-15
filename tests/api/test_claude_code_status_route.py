"""The claude_code discovery branch and the runtime_status route (the claude_code plan, task 2).

Discovery probes the CLI on the host this worker runs on (child_env scrubbed,
pass_env passed through) and answers the three model aliases; the route serves
the same check for a stored row and must never leak the CLI's account details.
"""

from __future__ import annotations

import socket

from tests.llm.claude_code.fake_claude import fake_claude  # noqa: F401  (fixture)


def _draft(fake_claude: str) -> dict:
    return {
        "provider": "claude_code",
        "config": {"binary_path": fake_claude, "pass_env": "FAKE_CLAUDE_LOGGED_IN"},
    }


async def test_discovery_returns_aliases_and_status(client, fake_claude):
    r = await client.post("/v1/llm_providers/_discover_models", json=_draft(fake_claude))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["models"] == ["sonnet", "opus", "haiku"]
    assert body["runtime"]["installed"] is True
    assert body["runtime"]["version"] == "2.1.322"
    assert body["runtime"]["logged_in"] is True
    assert body["runtime"]["host"] == socket.gethostname()


async def test_discovery_logged_out_is_400_naming_host(client, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_IN", "0")
    r = await client.post("/v1/llm_providers/_discover_models", json=_draft(fake_claude))
    assert r.status_code == 400
    assert socket.gethostname() in r.json()["detail"]


async def test_discovery_missing_binary_is_400_naming_host(client):
    r = await client.post(
        "/v1/llm_providers/_discover_models",
        json={"provider": "claude_code", "config": {"binary_path": "/nonexistent/claude"}},
    )
    assert r.status_code == 400
    assert socket.gethostname() in r.json()["detail"]


async def _register(client, provider_id: str, provider: str, config: dict) -> None:
    r = await client.post(
        "/v1/llm_providers",
        json={"id": provider_id, "provider": provider, "config": config, "limits": {"max_concurrency": 1}},
    )
    assert r.status_code in (200, 201), r.text


async def test_runtime_status_route_never_returns_email(client, fake_claude):
    """The fake prints an "email" (and org) field in its auth status; the response must carry no account details."""
    await _register(client, "cc-host", "claude_code", {"binary_path": fake_claude, "pass_env": "FAKE_CLAUDE_LOGGED_IN"})
    r = await client.get("/v1/llm_providers/cc-host/runtime_status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"host", "installed", "version", "logged_in", "meets_minimum"}
    assert body["installed"] is True and body["version"] == "2.1.322" and body["logged_in"] is True
    assert "@" not in r.text and "email" not in r.text


async def test_runtime_status_route_404s_for_missing_and_other_kinds(client, fake_claude):
    await _register(client, "cc-host", "claude_code", {"binary_path": fake_claude, "pass_env": "FAKE_CLAUDE_LOGGED_IN"})
    await _register(client, "prov-a", "anthropic", {"api_key": "sk-test-1"})
    assert (await client.get("/v1/llm_providers/nope/runtime_status")).status_code == 404
    assert (await client.get("/v1/llm_providers/prov-a/runtime_status")).status_code == 404
