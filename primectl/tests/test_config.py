import os
import stat
from pathlib import Path

import pytest

from primectl.config import (
    Config, Context, load_config, save_config, resolve_target, ConfigError,
)


def test_save_then_load_roundtrips(tmp_path: Path):
    cfg = Config(
        current_context="dogfood",
        contexts={"dogfood": Context(server="http://localhost:9000", token=None, workspace=None)},
    )
    p = tmp_path / "config.yaml"
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.current_context == "dogfood"
    assert loaded.contexts["dogfood"].server == "http://localhost:9000"


def test_save_sets_0600_perms(tmp_path: Path):
    p = tmp_path / "config.yaml"
    save_config(Config(current_context=None, contexts={}), p)
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600


def test_load_missing_returns_empty(tmp_path: Path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.current_context is None
    assert cfg.contexts == {}


def test_resolve_prefers_flags_over_context(tmp_path: Path):
    cfg = Config(
        current_context="dogfood",
        contexts={"dogfood": Context(server="http://ctx:9000", token="ctxtok", workspace="w1")},
    )
    t = resolve_target(cfg, context=None, server="http://flag:1", token="flagtok", env={})
    assert t.server == "http://flag:1"
    assert t.token == "flagtok"


def test_resolve_uses_current_context(tmp_path: Path):
    cfg = Config(
        current_context="dogfood",
        contexts={"dogfood": Context(server="http://ctx:9000", token=None, workspace=None)},
    )
    t = resolve_target(cfg, context=None, server=None, token=None, env={})
    assert t.server == "http://ctx:9000"
    assert t.token is None  # tokenless allowed


def test_resolve_env_token_reference(tmp_path: Path):
    cfg = Config(
        current_context="prod",
        contexts={"prod": Context(server="https://p", token="env:PRIMER_PROD_TOKEN", workspace=None)},
    )
    t = resolve_target(cfg, context=None, server=None, token=None, env={"PRIMER_PROD_TOKEN": "secret"})
    assert t.token == "secret"


def test_resolve_falls_back_to_env_var(tmp_path: Path):
    cfg = Config(
        current_context="dogfood",
        contexts={"dogfood": Context(server="http://ctx:9000", token=None, workspace=None)},
    )
    t = resolve_target(cfg, context=None, server=None, token=None, env={"PRIMER_API_TOKEN": "envtok"})
    assert t.token == "envtok"


def test_resolve_no_server_raises(tmp_path: Path):
    cfg = Config(current_context=None, contexts={})
    with pytest.raises(ConfigError):
        resolve_target(cfg, context=None, server=None, token=None, env={})
