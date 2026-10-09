"""``POST /v1/ssp/_test`` does not echo the typed database password in its validation error (ticket 01a11eda-29a9).

The draft route builds a transient ``SemanticSearchProvider`` and, when the config does not validate, answered ``{"ok": false, "error": "draft SSP config failed validation: <str(exc)>"}``.
pydantic's ``str(ValidationError)`` prints ``input_value=<the value as typed>``. For a MISSING field that value is the whole config dict (cut to its first 25 and last 24 characters), so a
draft with a missing field and the operator's real password in the same body came back with the password's tail in the 200 response; a wrong port prints only the port as typed, which is
why that case is a guard on the layout, not the leak. It is not a 422, so the app-wide drop of ``input`` from the 422 envelope (#645) never touches it: this route renders the
text itself. It now uses the layout of the error WITHOUT its input (``draft_error``, shared with the speech, web and embedding probes since #645): the person still learns WHICH field
is wrong and why.

The probe's own failure (``asyncpg.connect`` raising) goes through ``probe_error`` with the config, which also masks the password the row holds (defence in depth: a library's text for a
failed connect may print a secret that is not in URL form), on the draft route AND on the saved row's ``GET /v1/ssp/{id}/_test``.
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


def _connect_that_prints_the_password(password: str):
    """An ``asyncpg.connect`` whose failure text carries the password in a form no URL mask recognises (a quoted bare value, as a driver's detail line may print it)."""

    async def _connect(**kwargs):
        raise OSError(f"connection to server failed: password authentication failed, the server saw {password!r}")

    return _connect


@pytest.mark.asyncio
async def test_a_failed_connect_that_prints_the_password_is_masked_on_the_draft_route(client, monkeypatch) -> None:
    monkeypatch.setattr("asyncpg.connect", _connect_that_prints_the_password(PASSWORD))

    r = await client.post("/v1/ssp/_test", json=_pgvector())

    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, r.text
    assert PASSWORD not in r.text and "pw-0123" not in r.text, body["error"]
    assert body["error"].startswith("OSError: ") and "password authentication failed" in body["error"], body["error"]    # the reason stays


@pytest.mark.asyncio
async def test_a_failed_connect_that_prints_the_password_is_masked_on_the_saved_row_route(client, monkeypatch) -> None:
    """``GET /v1/ssp/{id}/_test`` probes the STORED row (the real password, server-side) and has the same failure text to clean."""
    monkeypatch.setattr("asyncpg.connect", _connect_that_prints_the_password(PASSWORD))
    create = await client.post("/v1/ssp", json={"id": "ssp-saved-probe", **_pgvector()})
    assert create.status_code == 201, create.text
    try:
        r = await client.get("/v1/ssp/ssp-saved-probe/_test")

        body = r.json()
        assert r.status_code == 200 and body["ok"] is False, r.text
        assert PASSWORD not in r.text and "pw-0123" not in r.text, body["error"]
        assert body["error"].startswith("OSError: ") and "password authentication failed" in body["error"], body["error"]
    finally:
        await client.delete("/v1/ssp/ssp-saved-probe")


@pytest.mark.asyncio
async def test_a_saved_row_probe_error_with_no_secret_in_it_reads_as_before(client, monkeypatch) -> None:
    async def _connect(**kwargs):
        raise ConnectionRefusedError("could not connect to server")

    monkeypatch.setattr("asyncpg.connect", _connect)
    create = await client.post("/v1/ssp", json={"id": "ssp-saved-probe-2", **_pgvector()})
    assert create.status_code == 201, create.text
    try:
        r = await client.get("/v1/ssp/ssp-saved-probe-2/_test")

        assert r.status_code == 200 and r.json() == {"ok": False, "error": "ConnectionRefusedError: could not connect to server"}, r.text
    finally:
        await client.delete("/v1/ssp/ssp-saved-probe-2")
