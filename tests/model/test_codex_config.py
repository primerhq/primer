"""The codex provider config (the codex plan, task 4).

The provider drives the operator's Codex CLI (``codex app-server``) on each
worker host as a model backend, so the row carries no credential of its own:
just the CLI path, an optional home directory, the environment variable
names the adapter may pass through to the child, and the operator's model
catalog file (section 6.6). A pass_env name that looks like a credential or
primer-internal plumbing is refused, the same screen as ClaudeCodeConfig.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.provider import CodexConfig, LLMProvider


def test_codex_config_defaults() -> None:
    config = CodexConfig()
    assert config.binary_path == "codex"
    assert config.codex_home is None
    assert config.pass_env == []
    assert config.model_catalog_path is None


def test_codex_row_coerces_config() -> None:
    row = LLMProvider.model_validate({"id": "cx", "provider": "codex",
        "config": {"pass_env": "HTTPS_PROXY, NO_PROXY"}, "limits": {"max_concurrency": 2}})
    assert isinstance(row.config, CodexConfig)
    assert row.config.binary_path == "codex" and row.config.pass_env == ["HTTPS_PROXY", "NO_PROXY"]


@pytest.mark.parametrize("name", ["PRIMER_DB__PASSWORD", "OPENAI_API_KEY", "GH_TOKEN", "my_secret", "DATABASE_URL"])
def test_pass_env_screens_secrets(name: str) -> None:
    with pytest.raises(ValidationError):
        CodexConfig(pass_env=[name])
