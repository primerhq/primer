"""A provider probe's failure never carries the Base URL's password (ticket 01a11c0d).

``httpx`` prints the whole request URL, userinfo included, in an error
(``Server error '500 ...' for url 'http://user:pw@host/v1/models'``), and the
probes interpolated that into the 400 they raise. That text went on to three
more places: the ``POST .../_discover_models`` answer, the ``last_error`` the
saved-provider route stamps on the stored row (kept until the next probe), and
the ``detail`` of the ``llm_provider`` predicate of ``GET /v1/setup/state``.

The fix is at the source: every message a probe raises goes through
``redact_url_secrets``. These tests drive the real routes and the real httpx
error text (respx only stands in for the upstream server). The hosted probes,
ollama's SDK and a pydantic validation error have no way to produce a
credentialed URL against a respx upstream today, so those cases make the
failing call raise an exception that carries one; they pin the wrap around the
call, not the library's wording.
"""
from __future__ import annotations

import httpx
import pytest
import respx

# The userinfo shapes a real Base URL can carry. The second is the awkward one:
# pydantic keeps the apostrophe raw and writes the password's "@" as %40, and
# httpx prints both.
_USERINFO = ["svc:s3cr3t-pw", "us'er:p@ss"]
_SECRETS = ("s3cr3t-pw", "p@ss", "p%40ss", "us'er")


def _url(userinfo: str, path: str = "/v1") -> str:
    return f"http://{userinfo}@127.0.0.1:8123{path}"


def _assert_clean(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text, f"{secret!r} leaked in {text!r}"


def _upstream_500() -> None:
    respx.route(method="GET", host="127.0.0.1", port=8123).mock(
        return_value=httpx.Response(500),
    )


async def _create_saved(client, provider_id: str, userinfo: str) -> None:
    r = await client.post(
        "/v1/llm_providers",
        json={
            "id": provider_id,
            "provider": "openresponses",
            "config": {"url": _url(userinfo), "flavor": "other", "api_key": "sk-x"},
            "limits": {"max_concurrency": 1},
        },
    )
    assert r.status_code in (200, 201), r.text


class TestDraftProbe:
    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("userinfo", _USERINFO)
    @pytest.mark.parametrize("provider", ["openresponses", "openchat"])
    async def test_a_500_from_the_upstream_does_not_echo_the_password(
        self, client, provider, userinfo,
    ) -> None:
        _upstream_500()
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={
                "provider": provider,
                "config": {"url": _url(userinfo), "flavor": "other"},
            },
        )
        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        # Still useful: what failed, where, and that credentials were sent.
        detail = r.json()["detail"]
        assert "probe failed" in detail
        assert "500 Internal Server Error" in detail
        assert "http://[REDACTED]@127.0.0.1:8123/v1/models" in detail

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_embedding_probe_does_not_echo_the_password(self, client) -> None:
        _upstream_500()
        r = await client.post(
            "/v1/embedding_providers/_discover_models",
            json={
                "provider": "openai",
                "config": {"url": _url("svc:s3cr3t-pw"), "flavor": "lmstudio"},
            },
        )
        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        assert "500 Internal Server Error" in r.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", [
        "http://svc:s3cr3t-pw@/v1",
        "http://svc:s3cr3t-pw@127.0.0.1:99999/v1",
        # pydantic cuts a long input to its first 25 and last 24 characters, which removes the "@" and leaves a slice of the password readable
        "http://admin:s3cr3t-pwhunter2hunter2@llm.example.com:808080/v1",
        # a raw "/", "?" or "#" in the password ends the userinfo for any URL-shaped mask, so the URL is not printed at all
        "http://svc:s3cr3t-pw/more@127.0.0.1:99999/v1",
        "http://svc:s3cr3t-pw?more@127.0.0.1:99999/v1",
        "http://svc:s3cr3t-pw#more@127.0.0.1:99999/v1",
    ])
    async def test_a_url_that_does_not_validate_does_not_echo_the_password(
        self, client, url,
    ) -> None:
        """pydantic prints ``input_value='<the URL as typed>'``: the draft's detail is built without the input."""
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={"provider": "openchat", "config": {"url": url, "flavor": "other"}},
        )
        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        detail = r.json()["detail"]
        for part in ("admin", "hunter2", "s3cr3t", "svc", "more@"):
            assert part not in detail, f"{part!r} in {detail!r}"
        assert "input_value" not in detail
        assert detail.startswith("Draft provider failed validation: 1 validation error for LLMProvider\n"), detail
        assert "\nurl\n  " in detail, "the field is still named, on its own line, with the reason indented under it"
        assert "[type=url_parsing, input_type=str]" in detail


class TestSavedProbe:
    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("userinfo", _USERINFO)
    async def test_the_400_and_the_stamped_last_error_do_not_echo_the_password(
        self, client, userinfo,
    ) -> None:
        await _create_saved(client, "saved-cred", userinfo)
        _upstream_500()
        r = await client.get("/v1/llm_providers/saved-cred/discovered_models")
        assert r.status_code == 400, r.text
        _assert_clean(r.text)

        got = (await client.get("/v1/llm_providers/saved-cred")).json()
        assert got["last_probe_ok"] is False
        # last_error is persisted on the row and stays until the next probe.
        _assert_clean(got["last_error"])
        assert "500 Internal Server Error" in got["last_error"]
        assert "http://[REDACTED]@127.0.0.1:8123/v1/models" in got["last_error"]


class TestSetupPredicate:
    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("userinfo", _USERINFO)
    async def test_the_llm_provider_detail_does_not_echo_the_password(
        self, client, userinfo,
    ) -> None:
        await _create_saved(client, "setup-cred", userinfo)
        _upstream_500()
        r = await client.get("/v1/setup/state")
        assert r.status_code == 200, r.text
        pred = {p["key"]: p for p in r.json()["predicates"]}["llm_provider"]
        assert pred["ok"] is False and pred["live"] is True
        _assert_clean(pred["detail"])
        assert "500 Internal Server Error" in pred["detail"]


class TestCallsThatCarryAUrlOnlyInTheException:
    """No respx upstream makes these libraries print a credentialed URL today,
    so the failing call is made to raise one."""

    @pytest.mark.asyncio
    async def test_ollama_does_not_echo_the_password(self, client, monkeypatch) -> None:
        import ollama

        async def _list(self, *a, **k):
            raise ConnectionError(
                f"Failed to connect to {_url('svc:s3cr3t-pw', '')} (is it running?)",
            )

        monkeypatch.setattr(ollama.AsyncClient, "list", _list)
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={"provider": "ollama", "config": {"url": _url("svc:s3cr3t-pw", "")}},
        )
        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        assert "ollama probe failed: ConnectionError" in r.json()["detail"]
        assert "(is it running?)" in r.json()["detail"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_real_ollama_error_keeps_its_status_and_has_no_password(
        self, client,
    ) -> None:
        """Characterisation, green before the fix too: ollama's own text for a
        500 names the status and not the URL."""
        _upstream_500()
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={"provider": "ollama", "config": {"url": _url("svc:s3cr3t-pw", "")}},
        )
        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        assert "(status code: 500)" in r.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider,fn", [
        ("openrouter", "_discover_openrouter_models"),
        ("anthropic", "_discover_anthropic_models"),
        ("gemini", "_discover_gemini_models"),
    ])
    @pytest.mark.parametrize("kind", ["network", "status"])
    async def test_a_hosted_probe_does_not_echo_a_credentialed_url(
        self, client, monkeypatch, provider, fn, kind,
    ) -> None:
        leaky = "https://svc:s3cr3t-pw@api.example.test/v1/models?key=AIza-SECRET-KEY"
        if kind == "network":
            exc: Exception = httpx.ConnectError(f"cannot reach {leaky}")
        else:
            request = httpx.Request("GET", "https://api.example.test/v1/models")
            exc = httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(500, text=f"retry {leaky}", request=request),
            )

        async def _raise(*a, **k):
            raise exc

        monkeypatch.setattr(f"primer.api.routers.providers.{fn}", _raise)
        r = await client.post(
            "/v1/llm_providers/_discover_models",
            json={"provider": provider, "config": {"api_key": "k-123"}},
        )
        assert r.status_code == 400, r.text
        _assert_clean(r.text)
        assert "AIza-SECRET-KEY" not in r.text
        assert "https://[REDACTED]@api.example.test/v1/models" in r.json()["detail"]
