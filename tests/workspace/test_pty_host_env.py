"""The local terminal's shell gets a curated environment, not the API server's (review W43, lead ruling D9).

The shell inherited ``os.environ`` whole, so a user with terminal access read the database URL and every provider key with
``env``. Exec, init commands and diagnostics already used a curated environment. The shell now keeps PATH, HOME, LANG,
LC_*, TERM, USER, SHELL and TZ from the server, plus everything the workspace's own env sets, and nothing else; TERM still
defaults to ``xterm-256color``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from primer.workspace.local.pty_host import LocalPtySession

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "openpty") or not Path("/usr/bin/env").exists(),
    reason="needs a POSIX pseudo-terminal and /usr/bin/env",
)


async def _shell_env(root: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a PTY session's child really gets: ``env`` run as the session's command, its output parsed."""
    session = LocalPtySession(root=root, env=env, cmd=["/usr/bin/env"])
    await session.start()
    out = b""

    async def _drain() -> None:
        nonlocal out
        async for chunk in session.output():
            out += chunk

    try:
        await asyncio.wait_for(_drain(), timeout=10)
    finally:
        await session.close()
    pairs = (line.split("=", 1) for line in out.decode(errors="replace").replace("\r\n", "\n").split("\n") if "=" in line)
    return {k: v for k, v in pairs}


# ---- characterization: what the shell keeps ---------------------------------------------------------------------------------------


async def test_the_workspace_env_reaches_the_shell(tmp_path) -> None:
    env = await _shell_env(tmp_path, {"WS_VAR": "from-the-workspace"})
    assert env["WS_VAR"] == "from-the-workspace"


async def test_term_defaults_to_xterm_256color(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    assert (await _shell_env(tmp_path))["TERM"] == "xterm-256color"


async def test_the_server_term_is_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TERM", "vt100")
    assert (await _shell_env(tmp_path))["TERM"] == "vt100"


async def test_the_workspace_env_wins_over_the_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TERM", "vt100")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = await _shell_env(tmp_path, {"TERM": "screen", "LANG": "de_DE.UTF-8"})
    assert (env["TERM"], env["LANG"]) == ("screen", "de_DE.UTF-8")


async def test_the_basics_are_kept(tmp_path, monkeypatch) -> None:
    kept = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/home/someone", "LANG": "C.UTF-8", "LC_TIME": "C",
        "LC_ALL": "C.UTF-8", "USER": "someone", "SHELL": "/bin/sh", "TZ": "UTC",
    }
    for name, value in kept.items():
        monkeypatch.setenv(name, value)
    env = await _shell_env(tmp_path)
    assert {name: env.get(name) for name in kept} == kept


async def test_a_variable_the_workspace_sets_itself_is_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRIMER_FAKE_DB_PASSWORD", "the-servers")
    env = await _shell_env(tmp_path, {"PRIMER_FAKE_DB_PASSWORD": "the-workspaces"})
    assert env["PRIMER_FAKE_DB_PASSWORD"] == "the-workspaces"


# ---- W43: the server's own variables stay out -------------------------------------------------------------------------------------


async def test_the_server_secrets_do_not_reach_the_shell(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRIMER_FAKE_DB_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-the-shell")
    monkeypatch.setenv("TMPDIR", "/tmp/server-tmp")
    env = await _shell_env(tmp_path)
    assert "PRIMER_FAKE_DB_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    assert "TMPDIR" not in env, "only the variables of the ruling are kept"
