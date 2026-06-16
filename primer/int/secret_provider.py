"""Abstract base class for Secret providers.

A *Secret provider* resolves named secrets (API keys, tokens, private
keys) to their values at runtime. It is the backing store for
``secret``-sourced workspace file mounts: a workspace template may
declare ``FileMount(source=_SecretSource(name="deploy_key"))`` and the
provider supplies the bytes without the value ever living in the
template row.

One application constructs one provider at startup, calls
``initialize`` once, then resolves secrets on demand:

.. code-block:: python

    provider = SecretProviderFactory.create(config)
    await provider.initialize()
    try:
        token = await provider.get_secret("deploy_key")
    finally:
        await provider.aclose()

Concrete impls bind to one backend (process env, Vault, k8s Secrets,
etc.). The default :class:`primer.secret.env.EnvSecretProvider` reads
process environment variables.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import SecretStr


class SecretProvider(ABC):
    """Backend-agnostic resolver for named secrets."""

    @abstractmethod
    async def initialize(self) -> None:
        """Open any backend connections. Idempotent."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release backend resources. Idempotent."""

    @abstractmethod
    async def get_secret(self, name: str) -> SecretStr | None:
        """Return the secret value for ``name``, or ``None`` if absent.

        Returning ``None`` (not raising) distinguishes a missing secret
        from a backend failure. Callers that require the secret raise
        their own error on ``None``.
        """
