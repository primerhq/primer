"""Explicit CI-only child entrypoint with a private Slack transport boundary.

All patches live only in this separately launched fixture process. Production
auth/routes/tools/worker/adapter/normalizer/router remain real. Socket Mode
handshake and real external credentials are outside this integration claim.
"""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def private_slack_transport(url: str) -> None:
    parsed = urlsplit(url)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
    assert parsed.path == "/api/" and not parsed.username and not parsed.password
    from primer.channel.slack import connection

    original_outbound = connection._build_outbound_app

    def local_connection(config):
        conn = original_outbound(config)
        conn.app.client.base_url = url
        conn.app.client.proxy = None
        conn.app.client.timeout = 5
        # Keep real Bolt/Slack SDK and adapter, with no Socket Mode network.
        assert conn.handler is None
        return conn

    connection._build_app_and_handler = local_connection
    connection._build_outbound_app = local_connection


def build_app(config_path: Path, slack_url: str):
    from primer.api.app import create_app
    from primer.api.config import AppConfig
    from primer.channel.slack.factory import _route_channel_event
    from primer.model.channel import Channel

    private_slack_transport(slack_url)
    config = AppConfig.model_validate(json.loads(config_path.read_text()))
    assert config.host == "127.0.0.1" and config.auth.enabled
    assert config.db.provider.value == "sqlite" and not config.auto_bootstrap
    assert Path(config.db.config.path).resolve().parent == config_path.resolve().parent
    app = create_app(config)

    @app.post("/__fixture__/channel-event/{channel_id}", include_in_schema=False)
    async def deliver(channel_id: str, request: Request):
        channel = await app.state.storage_provider.get_storage(Channel).get(channel_id)
        if channel is None:
            raise HTTPException(404, "unknown private fixture room")
        adapter = app.state.channel_registry.peek_adapter(channel_id)
        if adapter is None:
            # The test must take the documented human REST-save step; it
            # cannot manufacture listener readiness by lazy-building here.
            raise HTTPException(409, "save the room through REST to start its listener")
        raw = await request.json()
        assert raw["channel"] == channel.external_id
        accepted = await _route_channel_event(adapter, channel.provider_id, raw)
        return {"routed": accepted}

    return app, config


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--slack-url", required=True)
    args = parser.parse_args()
    app, config = build_app(args.config, args.slack_url)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning",
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
