"""Drive the ``primectl`` CLI as a subprocess against the live e2e server.

The cookbook ``*_cli.py`` e2e tests validate the recipes' published CLI path:
every setup step is performed with the exact ``primectl`` verbs the doc shows
(``create -f`` manifests, ``doc put``, ``session run``, ``workspace files
get``) against the running ``primer api`` instance on :8765, then the same
success outcome the API-driven test asserts is checked back.

This helper centralises the three things every CLI e2e needs:

* a bearer token minted against the live server (the CLI authenticates with
  ``Authorization: Bearer``; the e2e server is cookie-session auth, so we
  register + login over httpx once, mint a token, and hand it to the CLI);
* a ``Primectl`` runner that invokes ``primectl --server <url> --token <tok>``
  as a real subprocess (``PRIMECTL_CONFIG=/dev/null`` so no developer config
  leaks in), captures stdout/stderr, and surfaces a readable failure; and
* a ``manifest`` helper that writes a ``{kind, spec}`` envelope to a temp file
  for ``create -f`` (nested bodies like providers/agents/collections/graphs do
  not fit ``--set`` flat key=value pairs).

The deterministic LLM is the shared in-process ``mock_llm`` mock the live
server already reaches over HTTP (see ``tests/_support/mock_llm_fixtures.py``);
the CLI just creates a provider pointing at that mock's base_url, exactly as
``tests/_support/runs.make_scripted_agent`` does over the API.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

# The repo root is three parents up from this file (tests/_support/...).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRIMECTL_DIR = _REPO_ROOT / "primectl"

# The operator account the e2e bringup expects (mirrors tests/e2e/conftest).
_E2E_USER = {"username": "e2e", "password": "e2e-password-123"}


def mint_token(base_url: str, *, name: str) -> str:
    """Register (idempotent) + login the operator, then mint a bearer token.

    Returns the plaintext token the CLI uses for ``--token``. The e2e server
    is cookie-session auth, so we drive the auth flow over httpx once to obtain
    a token the request/response CLI can carry on every call.
    """
    with httpx.Client(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0)) as c:
        # register may 4xx if the user already exists; login establishes the
        # cookie session either way.
        try:
            c.post("/v1/auth/register", json=_E2E_USER)
        except httpx.HTTPError:
            pass
        r = c.post("/v1/auth/login", json=_E2E_USER)
        r.raise_for_status()
        r = c.post(
            "/v1/auth/tokens",
            json={"name": name, "scopes": ["admin"]},
        )
        r.raise_for_status()
        plaintext = r.json().get("plaintext")
    if not plaintext:
        raise RuntimeError("token mint returned no plaintext")
    return plaintext


@dataclass
class CliResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    def ok(self) -> "CliResult":
        if self.returncode != 0:
            raise AssertionError(
                f"primectl {' '.join(self.args)} exited {self.returncode}\n"
                f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
            )
        return self

    def json(self):
        return json.loads(self.stdout)


class Primectl:
    """A configured ``primectl`` subprocess runner bound to one server+token."""

    def __init__(self, server: str, token: str) -> None:
        self.server = server
        self.token = token

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> CliResult:
        """Invoke ``primectl <args>`` as a subprocess; assert success by default.

        ``primectl`` is run via ``uv run --project primectl`` so it executes
        from the in-repo source (no separate install step). The global flags
        ``--server``/``--token`` come BEFORE the subcommand (Typer root
        callback options).
        """
        cmd = [
            "uv", "run", "--project", str(_PRIMECTL_DIR), "primectl",
            "--server", self.server, "--token", self.token,
            *args,
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            env={
                **_clean_env(),
                # Never let a developer's ~/.primectl/config.yaml influence the run.
                "PRIMECTL_CONFIG": "/dev/null",
            },
            input=stdin,
            capture_output=True,
            text=True,
            timeout=300,
        )
        res = CliResult(
            args=list(args),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        if check:
            res.ok()
        return res


def _clean_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    # Drop a stray PRIMER_API_TOKEN so only the explicit --token wins.
    env.pop("PRIMER_API_TOKEN", None)
    return env


def manifest(tmp_path: Path, name: str, kind: str, spec: dict) -> str:
    """Write a ``{kind, spec}`` manifest to a temp file; return its path.

    Used for every ``primectl create -f`` step (nested bodies do not fit
    ``--set`` flat key=value pairs).
    """
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump({"kind": kind, "spec": spec}, sort_keys=False))
    return str(path)
