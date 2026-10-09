"""``POST /v1/ssp/_test`` does not echo the typed database password in its validation error (ticket 01a11eda-29a9).

The draft route builds a transient ``SemanticSearchProvider`` and, when the config does not validate, answered ``{"ok": false, "error": "draft SSP config failed validation: <str(exc)>"}``.
pydantic's ``str(ValidationError)`` prints ``input_value=<the value as typed>`` (the whole config dict, or a slice of it), so a draft with a wrong port and the operator's real password in
the same body came back with the password in the 200 response. It is not a 422, so the app-wide drop of ``input`` from the 422 envelope (#645) never touches it: this route renders the
text itself. It now uses the layout of the error WITHOUT its input (``draft_error``, shared with the speech, web and embedding probes since #645): the person still learns WHICH field
is wrong and why.
"""

from __future__ import annotations

import pytest

PASSWORD = "s3cr3t-db-pw-0123456789abcdefghij"


def _pgvector(**overrides) -> dict:
    config = {"hostname": "db.example.test", "port": 5432, "username": "primer", "password": PASSWORD, "database": "primer"}
    config.update(overrides)
    return {"provider": "pgvector", "config": config}


@pytest.mark.asyncio
async def test_a_draft_with_a_wrong_port_does_not_echo_the_password(client) -> None:
    r = await client.post("/v1/ssp/_test", json=_pgvector(port="notaport"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert PASSWORD not in r.text and "pw-0123" not in r.text, body["error"]
    assert "port" in body["error"], "the person still needs to know WHICH field is wrong"


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [PASSWORD, "pa'ss\"word/with:odd@chars-TAILSECRET-0123456789"])
async def test_a_draft_with_a_missing_field_does_not_echo_the_password(client, password: str) -> None:
    """A missing field makes the error's input the WHOLE config dict. pydantic prints its first 25 and LAST 24 characters, and ``password`` is the last key of the draft, so its tail
    is what shows."""
    draft = _pgvector(password=password)
    del draft["config"]["database"]

    r = await client.post("/v1/ssp/_test", json=draft)

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert password not in r.text and "0123456789" not in body["error"] and "TAILSECRET" not in body["error"], body["error"]
    assert "database" in body["error"]


@pytest.mark.asyncio
async def test_a_draft_of_an_unknown_provider_does_not_echo_its_config(client) -> None:
    r = await client.post("/v1/ssp/_test", json={"provider": "nonesuch", "config": {"hostname": "db.example.test", "password": PASSWORD, "port": 5432}})

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert PASSWORD not in r.text and "0123456789" not in body["error"], body["error"]


@pytest.mark.asyncio
async def test_the_error_keeps_pydantics_layout_without_the_input(client) -> None:
    r = await client.post("/v1/ssp/_test", json=_pgvector(port="notaport"))

    error = r.json()["error"]
    assert error.startswith("draft SSP config failed validation: "), error
    assert "validation error" in error and "input_value" not in error and "input_type" in error, error


@pytest.mark.asyncio
async def test_a_valid_draft_is_still_probed(client, monkeypatch) -> None:
    class _Conn:
        async def fetchval(self, query: str):
            return 1

        async def close(self) -> None:
            return None

    async def _connect(**kwargs):
        return _Conn()

    monkeypatch.setattr("asyncpg.connect", _connect)

    r = await client.post("/v1/ssp/_test", json=_pgvector())

    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
