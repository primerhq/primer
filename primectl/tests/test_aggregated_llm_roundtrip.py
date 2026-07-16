"""primectl forwards an aggregated LLMProvider manifest body verbatim."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()


def test_create_aggregated_llm_provider_posts_spec_verbatim(
    mock_session, tmp_path: Path,
):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "agg1"})

    mock_session.set_handler(handler)

    spec = {
        "id": "agg1",
        "provider": "aggregated",
        "config": {
            "members": [
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
            "strategy": "round_robin",
            "failover_point": "before_first_token",
            "failover_on": "transient_and_config",
        },
        "models": [{"name": "virtual-1", "context_length": 200000}],
        "limits": {"max_concurrency": 4},
    }
    manifest = tmp_path / "agg.yaml"
    # JSON is valid YAML; indent it two spaces so it nests under `spec:`.
    manifest.write_text(
        "kind: llm_provider\nspec:\n"
        + textwrap.indent(json.dumps(spec, indent=2), "  ")
        + "\n"
    )

    result = runner.invoke(app, ["create", "-f", str(manifest)], obj=mock_session.session)
    assert result.exit_code == 0, result.output
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/llm_providers"
    assert seen["body"] == spec   # nested config forwarded untouched
