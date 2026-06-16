import pytest

from primer.secret.env import EnvSecretProvider


@pytest.mark.asyncio
async def test_get_secret_present(monkeypatch):
    monkeypatch.setenv("PRIMER_SECRET_DEPLOY_KEY", "s3cr3t")
    provider = EnvSecretProvider()
    await provider.initialize()
    try:
        result = await provider.get_secret("deploy_key")
        assert result is not None
        assert result.get_secret_value() == "s3cr3t"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_get_secret_absent_returns_none(monkeypatch):
    monkeypatch.delenv("PRIMER_SECRET_MISSING", raising=False)
    provider = EnvSecretProvider()
    result = await provider.get_secret("missing")
    assert result is None


@pytest.mark.asyncio
async def test_name_is_uppercased(monkeypatch):
    monkeypatch.setenv("PRIMER_SECRET_TOKEN", "abc")
    provider = EnvSecretProvider()
    assert (await provider.get_secret("token")).get_secret_value() == "abc"
    assert (await provider.get_secret("TOKEN")).get_secret_value() == "abc"


@pytest.mark.asyncio
async def test_custom_prefix(monkeypatch):
    monkeypatch.setenv("APP_API_KEY", "xyz")
    provider = EnvSecretProvider(prefix="APP_")
    assert (await provider.get_secret("api_key")).get_secret_value() == "xyz"


from primer.model.provider import SecretProviderConfig, SecretProviderType
from primer.secret.factory import SecretProviderFactory


def test_factory_builds_env_provider():
    cfg = SecretProviderConfig()  # defaults to provider=env
    provider = SecretProviderFactory.create(cfg)
    assert isinstance(provider, EnvSecretProvider)


def test_factory_honors_custom_prefix():
    cfg = SecretProviderConfig(
        provider=SecretProviderType.ENV,
        config={"prefix": "APP_"},
    )
    provider = SecretProviderFactory.create(cfg)
    assert isinstance(provider, EnvSecretProvider)
    assert provider._prefix == "APP_"
