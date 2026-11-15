"""Shared claude CLI test double (the claude_code plan, task 2).

A stand-in for the operator's ``claude`` binary. The :func:`fake_claude` fixture
wraps this file in a ``#!/bin/sh`` executor; tests point ``binary_path`` at the
wrapper. Later claude_code tasks reuse the same double.

The surfaces it speaks:

- ``--version`` -> prints ``$FAKE_CLAUDE_VERSION`` (default ``2.1.322``) shaped
  like the real CLI's line;
- ``auth status`` -> prints the real CLI's JSON shape (``loggedIn``,
  ``authMethod``, ``email``, ``orgId``, ``orgName``). ``loggedIn`` comes from
  ``FAKE_CLAUDE_LOGGED_IN`` (``1``/``0``, default ``1``). The account fields are
  FAKE values that must never leak into primer's responses;
- anything else (the provider's stream call) -> drains stdin, then prints the
  NDJSON file named by ``FAKE_CLAUDE_FIXTURE``. With ``FAKE_CLAUDE_DELAY`` set
  (float seconds) it sleeps that long BEFORE each printed fixture line, so a
  test can stop the stream mid-way and watch the cleanup. With
  ``FAKE_CLAUDE_FIXTURE_SEQ`` (a comma-separated list of NDJSON paths) set
  instead, successive stream calls consume the list one fixture per
  invocation, counting through a ``.count`` file next to ``FAKE_CLAUDE_RECORD``
  (so the count survives the per-call process); a call past the end replays the
  last fixture.

With ``FAKE_CLAUDE_RECORD`` set, the double writes ``{"argv", "env", "stdin", "pid"}``
(plus ``tools_list`` when ``FAKE_CLAUDE_TOOLS_LIST`` is ``1``) there, BEFORE
any fixture line goes to stdout: a test that kills the CLI mid-stream (a tool
round, a Stop) still reads the record back. ``env`` is the child's WHOLE
environment, which is how a test pins what ``child_env`` scrubbed and what
``pass_env`` let through.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest


def _arg_after(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


async def _tools_list(mcp_config: str) -> list[dict]:
    """The ``tools/list`` of the first ``--mcp-config`` server, as the real CLI sees it."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    servers = json.loads(mcp_config).get("mcpServers", {})
    url = next(iter(servers.values())).get("url", "")
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.model_dump() for tool in result.tools]


def _fixture_path() -> str | None:
    """The fixture one stream invocation replays.

    ``FAKE_CLAUDE_FIXTURE_SEQ`` (a comma-separated list) wins: the invocations
    consume it one fixture at a time, in order, counting through a file next to
    ``FAKE_CLAUDE_RECORD`` (the count survives the per-call process); an
    invocation past the end replays the last fixture. Without the list, the
    single ``FAKE_CLAUDE_FIXTURE`` -- or nothing.
    """
    seq_raw = os.environ.get("FAKE_CLAUDE_FIXTURE_SEQ")
    if seq_raw:
        seq = [part.strip() for part in seq_raw.split(",") if part.strip()]
        if seq:
            record = os.environ.get("FAKE_CLAUDE_RECORD")
            anchor = Path(record) if record else Path(seq[0]).parent
            counter = anchor.with_name(anchor.name + ".count")
            try:
                index = int(counter.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                index = 0
            counter.write_text(str(index + 1), encoding="utf-8")
            return seq[min(index, len(seq) - 1)]
    return os.environ.get("FAKE_CLAUDE_FIXTURE")


def main() -> None:
    argv = sys.argv[1:]
    # CPython on macOS injects these into the process env at interpreter
    # startup (nothing the provider passed); drop them from the recorded env.
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in ("__CF_USER_TEXT_ENCODING", "LC_CTYPE")
    }
    if "--version" in argv:
        mode = "version"
    elif argv[:2] == ["auth", "status"]:
        mode = "auth_status"
    else:
        mode = "stream"

    # The stream case reads the whole conversation before answering (the real
    # CLI does the same); --version / auth status never touch stdin.
    stdin_text = sys.stdin.read() if mode == "stream" else ""

    tools = None
    if mode == "stream" and os.environ.get("FAKE_CLAUDE_TOOLS_LIST") == "1":
        mcp_config = _arg_after(argv, "--mcp-config")
        if mcp_config:
            tools = asyncio.run(_tools_list(mcp_config))

    if mode == "version":
        print(f"{os.environ.get('FAKE_CLAUDE_VERSION', '2.1.322')} (Claude Code)")
    elif mode == "auth_status":
        print(json.dumps(
            {
                "loggedIn": os.environ.get("FAKE_CLAUDE_LOGGED_IN", "1") == "1",
                "authMethod": "claudeai",
                "email": "fake@primer.example",
                "orgId": "org-fake-1",
                "orgName": "Primer Test Org",
            }
        ))
    else:
        # One fixture per stream invocation (FAKE_CLAUDE_FIXTURE_SEQ when set,
        # counting down through the counter file; FAKE_CLAUDE_FIXTURE otherwise).
        fixture = _fixture_path()
        if fixture:
            # The record (when asked for) is written BEFORE any stdout: a test
            # that kills the CLI mid-stream (a tool round, a Stop) still reads
            # it back.
            record_path = os.environ.get("FAKE_CLAUDE_RECORD")
            if record_path:
                # The FULL argv (argv[0] is the binary the provider exec'ed): a
                # test pins both the resolved binary and the flag order. ``pid``
                # is this (python) process: a test that kills the CLI
                # mid-stream checks that THIS pid is gone, not a host-wide
                # pgrep of the double's name.
                record: dict = {"argv": sys.argv, "env": env, "stdin": stdin_text, "pid": os.getpid()}
                if tools is not None:
                    record["tools_list"] = tools
                Path(record_path).write_text(json.dumps(record), encoding="utf-8")
            delay = float(os.environ.get("FAKE_CLAUDE_DELAY") or 0)
            for line in Path(fixture).read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                if delay:
                    time.sleep(delay)
                sys.stdout.write(line + "\n")
                sys.stdout.flush()


def write_fake_wrapper(tmp_path: Path) -> str:
    """The double wrapped in a ``#!/bin/sh`` executor the provider can exec as the CLI.

    The wrapper ``exec``s the python interpreter, so the recorded ``pid`` is the
    DIRECT child (the process-group leader), not an intermediate shell. The
    wrapper also unsets the names the POSIX shell adds on top of the environment
    the provider passed (``PWD``, ``SHLVL``, ``OLDPWD``, ``_``), and
    :func:`main` drops the two CPython injects at interpreter startup on macOS
    (``LC_CTYPE``, ``__CF_USER_TEXT_ENCODING``): the real ``claude`` is a
    non-Python binary that carries nothing of those, so the recorded env stays
    a faithful pin of what the provider gave the child. Shared by the
    :func:`fake_claude` fixture and the journey tests that point a live
    provider's ``binary_path`` at the double.
    """
    wrapper = tmp_path / "fake-claude"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec env -u PWD -u SHLVL -u OLDPWD -u _ {sys.executable} {Path(__file__).resolve()} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    """The double, wrapped in a shell script the provider can exec as the CLI."""
    return write_fake_wrapper(tmp_path)


if __name__ == "__main__":
    main()
