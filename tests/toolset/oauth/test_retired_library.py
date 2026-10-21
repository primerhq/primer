"""The shared MCP OAuth service fully replaces the old in-memory client."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from primer.model.provider import OAuthConfig

RETIRED = ("handler", "state", "token_store", "legacy", "registration", "discovery")


def test_no_module_imports_the_retired_oauth_library() -> None:
    for path in Path("primer").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in RETIRED:
            assert f"primer.toolset.oauth.{name}" not in text, path


def test_the_retired_modules_are_gone() -> None:
    for name in RETIRED:
        assert importlib.util.find_spec(f"primer.toolset.oauth.{name}") is None


def test_an_old_oauth_row_loads_without_its_retired_keys() -> None:
    config = OAuthConfig.model_validate(
        {
            "redirect_uri": "https://provider.example/callback",
            "spec_version": "2025-03-26",
        }
    )

    assert not hasattr(config, "redirect_uri")
    assert not hasattr(config, "spec_version")
    assert config.grant == "authorization_code"
