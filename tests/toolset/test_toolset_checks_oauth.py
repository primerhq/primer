"""OAuth-sensitive toolset edits share one comparison across REST and system tools."""

from __future__ import annotations

import pytest

from primer.model.provider import Toolset
from primer.toolset.toolset_checks import oauth_settings_changed


def _body(*, oauth: dict | None = None) -> dict:
    if oauth is None:
        oauth = {
            "grant": "authorization_code",
            "scopes": ["read"],
            "static_client": {
                "client_id": "client-1",
                "client_secret": "client-secret-test-only",
            },
        }
    return {
        "id": "kb",
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {
                "url": "https://mcp.example/mcp",
                "headers": {"X-Api-Key": "header-test-only"},
                "oauth": oauth,
            },
        },
    }


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("masked_round_trip", False),
        ("scopes", True),
        ("url", True),
        ("oauth_removed", True),
        ("client_secret_retyped", True),
        ("headers_only", False),
        ("existing_without_oauth", False),
    ],
)
def test_oauth_settings_changed(change: str, expected: bool) -> None:
    existing = Toolset.model_validate(_body())
    served = existing.model_dump(mode="json")

    if change == "scopes":
        served["config"]["config"]["oauth"]["scopes"] = ["read", "write"]
    elif change == "url":
        served["config"]["config"]["url"] = "https://mcp.example/v2/mcp"
    elif change == "oauth_removed":
        served["config"]["config"]["oauth"] = None
    elif change == "client_secret_retyped":
        served["config"]["config"]["oauth"]["static_client"]["client_secret"] = "replacement-test-only"
    elif change == "headers_only":
        served["config"]["config"]["headers"]["X-Trace"] = "trace-test-only"
    elif change == "existing_without_oauth":
        existing = Toolset.model_validate(_body(oauth={"grant": "authorization_code"}))
        existing.config.config.oauth = None
        served = existing.model_dump(mode="json")
        served["config"]["config"]["oauth"] = {"grant": "authorization_code", "scopes": ["read"]}

    entity = Toolset.model_validate(served)

    assert oauth_settings_changed(entity, existing) is expected
