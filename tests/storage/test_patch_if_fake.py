"""The shared in-memory fake must satisfy the same ``patch_if`` scenarios as the real backends.

The fake (``tests/conftest.py::_InMemoryStorage.patch_if``) is what every flag-on unit test leans on;
running the backend scenarios against it is what stops it quietly diverging.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.storage import _patch_scenarios as ps


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ps.ALL, ids=lambda f: f.__name__)
async def test_the_fake_satisfies_the_patch_if_contract(
    fake_storage_provider: Any, scenario: Any,
) -> None:
    await scenario(fake_storage_provider.get_storage(ps.PatchDoc))


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ps.RAW, ids=lambda f: f.__name__)
async def test_the_fake_satisfies_the_raw_document_patch_if_contract(
    fake_storage_provider: Any, scenario: Any,
) -> None:
    await scenario(ps.FakeEnv(fake_storage_provider))
