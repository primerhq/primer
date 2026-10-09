"""The 422 envelope never carries the INPUT of a field that did not validate (review of #645, B2).

``_validation_error_handler`` rendered ``exc.errors()`` whole, and pydantic puts the offending input in every error: a ``POST`` or ``PUT`` of an embedding provider with
``config.url = "http://svc:<password>@emb.local:badport/v1"`` answered ``errors[0].input`` with the password, and a ``huggingface`` row without a token answered ``input`` = the whole config
dict (the OpenAI key beside the missing token included). The input is what the caller typed, so it adds nothing a person needs; the console reads only ``loc`` and ``msg``. It is dropped
for every route, in the one handler, and the PUT path (``_crud.py`` re-raises a pydantic error into it) is covered by the same change.
"""

from __future__ import annotations

import pytest

PASSWORD = "s3cr3t-pw"
BAD_URL = f"http://svc:{PASSWORD}@emb.local:badport/v1"
OPENAI_KEY = "sk-secret-0123456789abcdefghijklmnopqrstuvwxyz"


def _row(provider: str, config: dict, row_id: str = "emb-a") -> dict:
    return {"id": row_id, "provider": provider, "models": [{"name": "text-embedding-3-small"}], "config": config, "limits": {"max_concurrency": 1}}


def _assert_no_input(r) -> list[dict]:
    assert r.status_code == 422, r.text
    errors = r.json()["extensions"]["errors"]
    assert errors, r.text
    for error in errors:
        assert "input" not in error, error
        assert {"type", "loc", "msg"} <= set(error), error
    return errors


@pytest.mark.asyncio
async def test_a_post_with_a_credentialed_url_that_does_not_validate_does_not_echo_it(client) -> None:
    r = await client.post("/v1/embedding_providers", json=_row("openai", {"url": BAD_URL}))

    errors = _assert_no_input(r)
    assert PASSWORD not in r.text and "pw@" not in r.text, r.text
    assert any(error["loc"][-1] == "url" for error in errors), "the person still needs to know WHICH field is wrong"


@pytest.mark.asyncio
async def test_a_put_with_a_credentialed_url_that_does_not_validate_does_not_echo_it(client) -> None:
    created = await client.post("/v1/embedding_providers", json=_row("openai", {"url": "http://emb.local:1234/v1"}))
    assert created.status_code in (200, 201), created.text

    r = await client.put("/v1/embedding_providers/emb-a", json=_row("openai", {"url": BAD_URL}))

    errors = _assert_no_input(r)
    assert PASSWORD not in r.text and "pw@" not in r.text, r.text
    assert any(error["loc"][-1] == "url" for error in errors)


@pytest.mark.asyncio
async def test_a_huggingface_row_without_a_token_does_not_echo_the_config_it_was_given(client) -> None:
    r = await client.post("/v1/embedding_providers", json=_row("huggingface", {"url": "http://x.local/v1", "api_key": OPENAI_KEY}))

    errors = _assert_no_input(r)
    assert OPENAI_KEY not in r.text and "sk-secret" not in r.text, r.text
    assert any(error["loc"][-1] == "token" for error in errors)


@pytest.mark.asyncio
async def test_a_put_of_a_huggingface_row_without_a_token_does_not_echo_the_config_either(client) -> None:
    created = await client.post("/v1/embedding_providers", json=_row("huggingface", {"token": "hf_token_0123456789"}))
    assert created.status_code in (200, 201), created.text

    r = await client.put("/v1/embedding_providers/emb-a", json=_row("huggingface", {"url": "http://x.local/v1", "api_key": OPENAI_KEY}))

    _assert_no_input(r)
    assert OPENAI_KEY not in r.text and "sk-secret" not in r.text, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/embedding_providers", {}),
        ("/v1/embedding_providers", {"id": 5, "provider": ["x"], "models": "no", "config": 7, "limits": None}),
        ("/v1/agents", {"id": "a", "model": "not an object"}),
    ],
)
async def test_every_route_drops_the_input_and_keeps_type_loc_and_msg(client, path: str, body: dict) -> None:
    r = await client.post(path, json=body)

    _assert_no_input(r)


@pytest.mark.asyncio
async def test_the_envelope_keeps_its_shape(client) -> None:
    r = await client.post("/v1/embedding_providers", json={})

    body = r.json()
    assert r.status_code == 422
    assert body["title"] == "Validation Error" and body["type"].endswith("/validation-error"), body
    assert body["detail"] == "One or more request parameters or body fields failed validation."
    assert all(isinstance(error["loc"], list) and isinstance(error["msg"], str) for error in body["extensions"]["errors"])
