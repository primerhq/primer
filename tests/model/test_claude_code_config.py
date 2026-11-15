"""The claude_code provider config (the claude_code plan, task 1).

The provider drives the operator's logged-in ``claude`` CLI as a model
backend, so the row carries no credential of its own: just the CLI path
and the environment variable names the adapter may pass through to the
child. A name that looks like a credential or primer-internal plumbing
is refused, with the error naming the offending variable.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.provider import ClaudeCodeConfig, LLMProvider


def test_claude_code_row_coerces_config():
    row = LLMProvider.model_validate({"id": "cc", "provider": "claude_code",
        "config": {"pass_env": "HTTPS_PROXY, NO_PROXY"}, "limits": {"max_concurrency": 2}})
    assert isinstance(row.config, ClaudeCodeConfig)
    assert row.config.binary_path == "claude" and row.config.pass_env == ["HTTPS_PROXY", "NO_PROXY"]


@pytest.mark.parametrize("name", ["PRIMER_DB__PASSWORD", "OPENAI_API_KEY", "GH_TOKEN", "my_secret", "DATABASE_URL"])
def test_pass_env_refuses_secret_names(name):
    with pytest.raises(ValidationError):
        ClaudeCodeConfig(pass_env=[name])
