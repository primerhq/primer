"""`primectl capabilities` renders the server's optional-extra status."""

import httpx
from typer.testing import CliRunner

from primectl.main import app

runner = CliRunner()

_PAYLOAD = {
    "version": "9.9.9",
    "extras": {
        "channels": {
            "installed": False,
            "platforms": {"slack": True, "telegram": False, "discord": False},
        },
        "docker": {"installed": True, "platforms": None},
        "lance": {"installed": False, "platforms": None},
    },
}


def test_capabilities_calls_the_endpoint_and_renders_rows(mock_session):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=_PAYLOAD)

    mock_session.set_handler(handler)
    result = runner.invoke(app, ["capabilities"], obj=mock_session.session)

    assert result.exit_code == 0, result.output
    assert seen["path"] == "/v1/capabilities"
    assert "docker" in result.output
    assert "lance" in result.output


def test_capabilities_shows_which_channel_platforms_are_present(mock_session):
    """A partial channels install must not collapse to a bare "no".

    'channels' counts as installed only when all three SDKs import, so
    without the per-platform detail an operator with Slack working would
    see the same output as one with nothing installed.
    """
    mock_session.set_handler(lambda request: httpx.Response(200, json=_PAYLOAD))
    result = runner.invoke(app, ["capabilities"], obj=mock_session.session)

    assert result.exit_code == 0, result.output
    assert "slack" in result.output
