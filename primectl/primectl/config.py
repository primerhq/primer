"""primectl configuration: contexts file + target resolution.

The config lives at ``~/.primectl/config.yaml`` (override with the
``PRIMECTL_CONFIG`` env var). A context is (server, token, workspace). Tokens
may be inline strings or ``env:VARNAME`` references so secrets stay out of the
file. ``resolve_target`` applies precedence: flags > context > env > default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised on unusable configuration (e.g. no server resolvable)."""


@dataclass
class Context:
    server: str
    token: str | None = None
    workspace: str | None = None


@dataclass
class Config:
    current_context: str | None
    contexts: dict[str, Context]


@dataclass
class Target:
    server: str
    token: str | None
    workspace: str | None
    context_name: str | None


def config_path() -> Path:
    override = os.environ.get("PRIMECTL_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".primectl" / "config.yaml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config(current_context=None, contexts={})
    data = yaml.safe_load(path.read_text()) or {}
    contexts = {
        name: Context(
            server=c.get("server", ""),
            token=c.get("token"),
            workspace=c.get("workspace"),
        )
        for name, c in (data.get("contexts") or {}).items()
    }
    return Config(current_context=data.get("current-context"), contexts=contexts)


def save_config(config: Config, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "current-context": config.current_context,
        "contexts": {
            name: {
                "server": c.server,
                "token": c.token,
                "workspace": c.workspace,
            }
            for name, c in config.contexts.items()
        },
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    path.chmod(0o600)


def _deref_token(token: str | None, env: dict[str, str]) -> str | None:
    if token is None:
        return None
    if token.startswith("env:"):
        return env.get(token[4:])
    return token


def resolve_target(
    config: Config,
    *,
    context: str | None,
    server: str | None,
    token: str | None,
    env: dict[str, str] | None = None,
) -> Target:
    env = env if env is not None else dict(os.environ)
    ctx_name = context or config.current_context
    ctx = config.contexts.get(ctx_name) if ctx_name else None

    eff_server = server or (ctx.server if ctx else None)
    if not eff_server:
        raise ConfigError(
            "no server: pass --server, set a current context with "
            "'primectl config use-context', or define one with "
            "'primectl config set-context'."
        )

    eff_token = token
    if eff_token is None and ctx is not None:
        eff_token = _deref_token(ctx.token, env)
    if eff_token is None:
        eff_token = env.get("PRIMER_API_TOKEN")

    return Target(
        server=eff_server,
        token=eff_token,
        workspace=ctx.workspace if ctx else None,
        context_name=ctx_name,
    )
