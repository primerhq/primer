"""The harness tools serve a git_url without its password and refuse a registration that carries the served mask (ticket 01a11d32, the harness family).

``harness__list`` and ``harness__get`` are user-tier: their results go into a transcript and to the model vendor. ``harness__register`` is the create: an agent that reads a harness and registers
it again under a new slug (the copy-a-harness move) would store the literal mask as the password of the URL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.harness import Harness, HarnessStatus
from primer.toolset.harness import build_harness_toolset_provider
from tests.toolset.test_harness_toolset import _SP, _EventBus

MASK = "**********"
URL = "https://reader:s3cr3t@git.example.com/org/repo.git"
MASKED = f"https://reader:{MASK}@git.example.com/org/repo.git"

pytestmark = pytest.mark.asyncio


def _provider(sp):
    return build_harness_toolset_provider(storage_provider=sp, event_bus=_EventBus())


async def _seeded() -> tuple[_SP, object]:
    sp = _SP()
    await sp.get_storage(Harness).create(
        Harness(id="hns_1", slug="url-harness", name="x", git_url=URL, status=HarnessStatus.READY, created_at=datetime.now(timezone.utc)),
    )
    return sp, _provider(sp)


async def test_get_and_list_results_carry_no_password() -> None:
    sp, provider = await _seeded()

    got = await provider.call(tool_name="harness__get", arguments={"id": "hns_1"})
    listed = await provider.call(tool_name="harness__list", arguments={})

    assert not got.is_error and not listed.is_error, (got.output, listed.output)
    assert "s3cr3t" not in got.output + listed.output
    assert json.loads(got.output)["git_url"] == MASKED
    assert (await sp.get_storage(Harness).get("hns_1")).git_url == URL


async def test_registering_with_the_url_a_get_returned_is_refused_and_stores_nothing() -> None:
    sp, provider = await _seeded()
    served = json.loads((await provider.call(tool_name="harness__get", arguments={"id": "hns_1"})).output)

    result = await provider.call(tool_name="harness__register", arguments={"name": "Copy", "slug": "copy-harness", "git_url": served["git_url"]})

    assert result.is_error, result.output
    assert json.loads(result.output)["type"] == "validation-error" and "re-enter the password" in result.output
    assert [h.id for h in (await sp.get_storage(Harness).list(None)).items] == ["hns_1"]


async def test_registering_with_a_real_credential_is_fine_and_served_masked() -> None:
    sp = _SP()
    provider = _provider(sp)

    result = await provider.call(tool_name="harness__register", arguments={"name": "New", "slug": "new-harness", "git_url": URL})

    assert not result.is_error, result.output
    assert json.loads(result.output)["git_url"] == MASKED and "s3cr3t" not in result.output
    [row] = (await sp.get_storage(Harness).list(None)).items
    assert row.git_url == URL
