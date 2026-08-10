"""Unit tests for the extras capability helper (modular-monolith spec)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import primer.common.optional as optional_mod
from primer.common.optional import (
    CHANNEL_PLATFORM_MODULES,
    EXTRA_MODULES,
    channel_platforms,
    has_extra,
    install_hint,
    require_extra,
)
from primer.model.except_ import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_extra_modules_matches_pyproject_extras() -> None:
    """EXTRA_MODULES keys stay in lockstep with pyproject extras (minus 'full')."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)
    extras = set(pyproject["project"]["optional-dependencies"]) - {"full"}
    assert set(EXTRA_MODULES) == extras


def test_has_extra_reflects_find_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: object())
    assert has_extra("lance") is True
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: None)
    assert has_extra("lance") is False


def test_has_extra_channels_requires_all_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    present = {"slack_bolt": object(), "telegram": object()}
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: present.get(name))
    assert has_extra("channels") is False
    assert channel_platforms() == {"slack": True, "telegram": True, "discord": False}


def test_has_extra_unknown_extra_raises() -> None:
    with pytest.raises(KeyError):
        has_extra("nonsense")


def test_install_hint_names_extra_and_restart() -> None:
    hint = install_hint("lance")
    assert "pip install 'primer-ai[lance]'" in hint
    assert "restart" in hint


def test_require_extra_raises_configerror_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: None)
    with pytest.raises(ConfigError) as exc_info:
        require_extra("kubernetes", "the Kubernetes workspace backend")
    msg = str(exc_info.value)
    assert "the Kubernetes workspace backend" in msg
    assert "'kubernetes' extra" in msg
    assert "pip install 'primer-ai[kubernetes]'" in msg


def test_require_extra_noop_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(optional_mod, "_find_spec", lambda name: object())
    require_extra("docker", "the container workspace backend")  # no raise
