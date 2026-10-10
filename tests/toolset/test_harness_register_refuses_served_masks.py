"""``harness__register`` refuses a ``git_token`` that is the mask a ``harness__get`` served (ticket 01a1212a, round 3 of #711, nit N2).

``POST /v1/harnesses`` refuses it since round 1; the harness toolset has its own create tool (``harness__register``) that wraps the string in a ``SecretStr`` and stored the literal mask as the git
token, so the copy-a-harness move through the tools (``harness__get``, a new slug, ``harness__register``) did what the REST route no longer does. It is ``type=validation-error`` now and nothing is
stored; a real token and no token work as they did.
"""

from __future__ import annotations

import json

import pytest

from primer.model.harness import Harness
from primer.toolset.harness import build_harness_toolset_provider
from tests.toolset.test_harness_toolset import _SP, _EventBus

pytestmark = pytest.mark.asyncio


def _provider(sp):
    return build_harness_toolset_provider(storage_provider=sp, event_bus=_EventBus())


def _args(**extra) -> dict:
    return {"name": "Copy", "slug": "copy-of-harness", "git_url": "https://github.com/example/repo", **extra}


@pytest.mark.parametrize("served", ["**********", "**********cdef"], ids=["the bare mask", "the mask with the last four characters"])
async def test_a_register_that_carries_the_served_token_mask_is_refused_and_stores_nothing(served: str) -> None:
    sp = _SP()

    result = await _provider(sp).call(tool_name="harness__register", arguments=_args(git_token=served))

    assert result.is_error, result.output
    assert json.loads(result.output)["type"] == "validation-error" and "re-enter" in result.output
    assert (await sp.get_storage(Harness).list(None)).items == []


async def test_a_register_of_a_real_token_still_works_and_serves_it_masked() -> None:
    sp = _SP()

    result = await _provider(sp).call(tool_name="harness__register", arguments=_args(git_token="ghp_real_token_0123456789"))

    assert not result.is_error, result.output
    assert json.loads(result.output)["git_token"] == "**********"
    [row] = (await sp.get_storage(Harness).list(None)).items
    assert row.git_token.get_secret_value() == "ghp_real_token_0123456789"


async def test_a_register_with_no_token_still_works() -> None:
    result = await _provider(_SP()).call(tool_name="harness__register", arguments=_args())

    assert not result.is_error, result.output
