"""Which key seals the MCP OAuth tokens: ``PRIMER_ENCRYPTION_KEY``, else the session secret with a warning (plan 07, Task 4; design 12.6).

A deployment key is the real protection: the session secret lives in the same database as the sealed tokens, so a key derived from it only obfuscates them.
The fallback exists so a deployment that has not set a key yet still works, and it says so, once per process start. A key that is too short is refused at boot,
because a weak key would look like protection and not be.
"""

from __future__ import annotations

import logging

import pytest

from primer.api.config import AppConfig, AuthConfig
from primer.auth.secret import SECRET_BOX_FALLBACK_WARNING, resolve_secret_box
from primer.common.secret_box import SecretBox
from primer.model.except_ import ConfigError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A developer's own PRIMER_ENCRYPTION_KEY or session secret must not decide these tests."""
    monkeypatch.delenv("PRIMER_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("PRIMER_AUTH__SESSION_SECRET", raising=False)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == "primer.auth.secret"]


async def test_resolve_uses_the_env_key(fake_storage_provider, caplog):
    caplog.set_level(logging.WARNING)
    box = await resolve_secret_box(AppConfig(encryption_key="k" * 32), fake_storage_provider)
    assert SECRET_BOX_FALLBACK_WARNING not in caplog.text
    assert _warnings(caplog) == []
    assert SecretBox.from_key_material("k" * 32).decrypt(box.encrypt("tok")) == "tok"
    # the session secret was not even generated: the env key needs nothing from the database
    assert (await fake_storage_provider.get_system_state()).session_secret is None


async def test_resolve_falls_back_to_the_session_secret_with_a_warning(fake_storage_provider, caplog):
    caplog.set_level(logging.WARNING)
    box = await resolve_secret_box(AppConfig(), fake_storage_provider)
    assert SECRET_BOX_FALLBACK_WARNING in caplog.text
    assert _warnings(caplog) == [SECRET_BOX_FALLBACK_WARNING]
    session_secret = (await fake_storage_provider.get_system_state()).session_secret
    assert session_secret, "the fallback key material is the session secret, generated and persisted on first need"
    assert SecretBox.from_key_material(session_secret).decrypt(box.encrypt("tok")) == "tok"


async def test_the_fallback_key_is_stable_across_resolutions(fake_storage_provider, caplog):
    caplog.set_level(logging.WARNING)
    first = await resolve_secret_box(AppConfig(), fake_storage_provider)
    second = await resolve_secret_box(AppConfig(), fake_storage_provider)
    assert second.decrypt(first.encrypt("tok")) == "tok"
    assert len(_warnings(caplog)) == 2, "each process start says it once"


async def test_the_fallback_uses_the_operators_session_secret_when_there_is_one(fake_storage_provider):
    box = await resolve_secret_box(AppConfig(auth=AuthConfig(session_secret="operator-session-secret")), fake_storage_provider)
    assert SecretBox.from_key_material("operator-session-secret").decrypt(box.encrypt("tok")) == "tok"


async def test_the_env_key_wins_over_the_session_secret(fake_storage_provider):
    config = AppConfig(encryption_key="e" * 20, auth=AuthConfig(session_secret="operator-session-secret"))
    box = await resolve_secret_box(config, fake_storage_provider)
    assert SecretBox.from_key_material("e" * 20).decrypt(box.encrypt("tok")) == "tok"


async def test_a_short_env_key_is_refused(fake_storage_provider):
    with pytest.raises(ConfigError, match="PRIMER_ENCRYPTION_KEY must be at least 16 characters"):
        await resolve_secret_box(AppConfig(encryption_key="short"), fake_storage_provider)


async def test_sixteen_characters_is_enough_and_fifteen_is_not(fake_storage_provider):
    await resolve_secret_box(AppConfig(encryption_key="s" * 16), fake_storage_provider)
    with pytest.raises(ConfigError):
        await resolve_secret_box(AppConfig(encryption_key="s" * 15), fake_storage_provider)


async def test_an_empty_env_key_is_refused_not_ignored(fake_storage_provider):
    """``PRIMER_ENCRYPTION_KEY=`` is a manifest that lost its value: failing at boot beats silently sealing with the fallback."""
    with pytest.raises(ConfigError):
        await resolve_secret_box(AppConfig(encryption_key=""), fake_storage_provider)


async def test_the_refusal_does_not_repeat_the_key(fake_storage_provider):
    with pytest.raises(ConfigError) as caught:
        await resolve_secret_box(AppConfig(encryption_key="tiny-key"), fake_storage_provider)
    assert "tiny-key" not in str(caught.value)


def test_the_env_var_name(monkeypatch):
    monkeypatch.setenv("PRIMER_ENCRYPTION_KEY", "x" * 20)
    assert AppConfig().encryption_key.get_secret_value() == "x" * 20


def test_the_key_is_unset_by_default():
    assert AppConfig().encryption_key is None


def test_the_config_never_shows_the_key():
    config = AppConfig(encryption_key="visible-nowhere-0123456789")
    assert "visible-nowhere" not in repr(config)
    assert "visible-nowhere" not in config.model_dump_json()
    assert "visible-nowhere" not in str(config.model_dump(mode="json"))
