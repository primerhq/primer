"""Stand up the slow-streaming OpenAI-compatible mock LLM as a real,
standalone HTTP server for manual/live diagnostic use.

tests/_support/mock_llm.py's ``mock_llm`` pytest fixture only runs inside
a pytest process, bound to 127.0.0.1 - fine for in-process e2e tests, but
unreachable from a docker/podman-compose container (e.g. the :8765
ui-bringup.sh stack) sitting in its own network namespace. This script
runs the SAME app standalone, bound to 0.0.0.0 by default, with the
reusable slow_turn_with_mid_stream_tool_call() scenario pre-registered -
the shape 01a04d64-b4ba's live diagnosis needed (a real, 10-20s,
multi-round-trip turn with a tool call in the middle) and had no way to
get without a real, rate-limited, sometimes-unreachable provider.

Usage:
    uv run python scripts/e2e/run_mock_llm.py [--port 8899] [--host 0.0.0.0]
        [--total-seconds 15] [--tool-name misc__uuid_v4] [--model scripted:slow]

Then register a REAL llm_providers row against it from the target stack:
    POST /v1/llm_providers
    {"id": "...", "provider": "openchat",
     "models": [{"name": "scripted:slow", "context_length": 8192}],
     "config": {"url": "http://<this-host-LAN-IP>:8899/v1", "flavor": "other"}}

Bind an agent's model to that provider/model pair and start a real
session turn - turn_status will read "running" for the whole ~15s
window (01a04d91-a7a0's fix), with a genuine mid-turn tool call.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from tests._support.mock_llm import (  # noqa: E402
    ScriptRegistry,
    build_app,
    slow_turn_with_mid_stream_tool_call,
)


def _guess_lan_ip() -> str | None:
    """Best-effort LAN IP for the printed registration hint. Never
    raises - a diagnostic convenience, not load-bearing."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--model", default="scripted:slow")
    parser.add_argument("--tool-name", default="misc__uuid_v4")
    parser.add_argument("--total-seconds", type=float, default=15.0)
    args = parser.parse_args()

    registry = ScriptRegistry()
    registry.register(
        args.model,
        slow_turn_with_mid_stream_tool_call(
            tool_name=args.tool_name, total_seconds=args.total_seconds,
        ),
    )

    lan_ip = _guess_lan_ip()
    print(f"[run_mock_llm] scenario model id: {args.model!r}", file=sys.stderr)
    print(f"[run_mock_llm] listening on http://{args.host}:{args.port}/v1", file=sys.stderr)
    if lan_ip:
        print(
            f"[run_mock_llm] from a compose/k8s container, register "
            f"config.url=http://{lan_ip}:{args.port}/v1", file=sys.stderr,
        )
    print(
        f"[run_mock_llm] turn shape: ~{args.total_seconds:.0f}s total, "
        f"tool call ~{args.total_seconds / 2:.0f}s in, then a slow "
        "multi-chunk final answer", file=sys.stderr,
    )

    uvicorn.run(build_app(registry), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
