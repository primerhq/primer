"""REST tests for /v1/stt_providers and /v1/tts_providers (S4 P1 Task 10)."""

from __future__ import annotations

import pytest


_STT = {
    "id": "stt-a",
    "provider": "openai",
    "default_model": "whisper-1",
    "config": {"url": "http://asr.local:8006/v1", "api_key": "sk-secret-XXXX"},
    "limits": {"max_concurrency": 2},
}

_TTS = {
    "id": "tts-a",
    "provider": "openai",
    "default_model": "kokoro",
    "default_voice": "af_heart",
    "config": {"url": "http://tts.local:8004/v1"},
    "limits": {"max_concurrency": 2},
}


class TestSttCrud:
    @pytest.mark.asyncio
    async def test_list_empty(self, client) -> None:
        r = await client.get("/v1/stt_providers?limit=10")
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_create_redacts_the_api_key_in_the_response(self, client) -> None:
        r = await client.post("/v1/stt_providers", json=_STT)
        assert r.status_code in (200, 201), r.text
        assert "sk-secret-XXXX" not in r.text

    @pytest.mark.asyncio
    async def test_create_then_get_round_trips(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        r = await client.get("/v1/stt_providers/stt-a")
        assert r.status_code == 200, r.text
        assert r.json()["default_model"] == "whisper-1"

    @pytest.mark.asyncio
    async def test_unknown_provider_type_is_422(self, client) -> None:
        body = dict(_STT, id="stt-bad", provider="deepgram")
        r = await client.post("/v1/stt_providers", json=body)
        assert r.status_code == 422, r.text

    @pytest.mark.asyncio
    async def test_delete_removes_the_row(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        r = await client.delete("/v1/stt_providers/stt-a")
        assert r.status_code in (200, 204), r.text
        assert (await client.get("/v1/stt_providers/stt-a")).status_code == 404


class TestTtsCrud:
    @pytest.mark.asyncio
    async def test_create_then_get_round_trips(self, client) -> None:
        r = await client.post("/v1/tts_providers", json=_TTS)
        assert r.status_code in (200, 201), r.text
        got = await client.get("/v1/tts_providers/tts-a")
        assert got.json()["default_voice"] == "af_heart"


def _keys(fields) -> list[str]:
    """Field specs are either a bare key or {key, label, required, ...}."""
    return [f["key"] if isinstance(f, dict) else f for f in fields]


def _required(fields) -> set[str]:
    return {
        f["key"] for f in fields
        if isinstance(f, dict) and f.get("required")
    }


# The two draft ``_test`` routes and the probe each one calls (the names are looked up in the router module, which is where a test replaces them).
_PROBES = [
    pytest.param("stt_providers", "list_models", id="stt"),
    pytest.param("tts_providers", "list_voices", id="tts"),
]


def _patch_probe(monkeypatch, attr: str, *, answer=None, error: Exception | None = None) -> list[dict]:
    """Stand in for ``primer.api.routers.speech.<attr>``: it records what it was asked to probe, then answers ``answer`` or raises ``error``."""
    calls: list[dict] = []

    async def _probe(*, url, api_key, timeout=10.0):
        calls.append({"url": url, "api_key": api_key})
        if error is not None:
            raise error
        return answer

    monkeypatch.setattr(f"primer.api.routers.speech.{attr}", _probe)
    return calls


class TestHelperRoutesBeatTheIdPattern:
    @pytest.mark.asyncio
    async def test_stt_types_is_not_captured_as_an_id(self, client) -> None:
        r = await client.get("/v1/stt_providers/_types")
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"openai"}
        spec = r.json()["openai"]
        # Fields carry their required-ness now, so compare KEYS and then
        # the flag: url and default_model are required on the model, and
        # a form told otherwise offers Save on a row the server rejects.
        assert _keys(spec["config_fields"]) == ["url", "api_key"]
        assert _keys(spec["row_fields"]) == ["default_model"]
        assert _required(spec["config_fields"]) == {"url"}
        assert _required(spec["row_fields"]) == {"default_model"}

    @pytest.mark.asyncio
    async def test_tts_types_lists_the_voice_field(self, client) -> None:
        r = await client.get("/v1/tts_providers/_types")
        assert r.status_code == 200, r.text
        spec = r.json()["openai"]
        assert _keys(spec["row_fields"]) == ["default_model", "default_voice"]
        assert _required(spec["row_fields"]) == {"default_model", "default_voice"}

    @pytest.mark.asyncio
    async def test_stt_test_reports_ok_with_the_model_list(self, client, monkeypatch) -> None:
        async def _fake_list_models(*, url, api_key, timeout=10.0):
            return ["whisper-1"]

        monkeypatch.setattr(
            "primer.api.routers.speech.list_models", _fake_list_models,
        )
        r = await client.post("/v1/stt_providers/_test", json=_STT)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "models": ["whisper-1"]}

    @pytest.mark.asyncio
    async def test_stt_test_reports_the_failure_without_raising(self, client, monkeypatch) -> None:
        async def _boom(*, url, api_key, timeout=10.0):
            raise RuntimeError("connection refused")

        monkeypatch.setattr("primer.api.routers.speech.list_models", _boom)
        r = await client.post("/v1/stt_providers/_test", json=_STT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "connection refused" in body["error"]

    @pytest.mark.asyncio
    async def test_tts_test_returns_the_voice_list(self, client, monkeypatch) -> None:
        async def _fake_list_voices(*, url, api_key, timeout=10.0):
            return ["af_heart", "am_adam"]

        monkeypatch.setattr(
            "primer.api.routers.speech.list_voices", _fake_list_voices,
        )
        r = await client.post("/v1/tts_providers/_test", json=_TTS)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "voices": ["af_heart", "am_adam"]}

    @pytest.mark.asyncio
    async def test_an_invalid_draft_is_reported_not_raised(self, client) -> None:
        r = await client.post(
            "/v1/stt_providers/_test",
            json={"id": "x", "provider": "openai", "config": {}},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False
        assert "invalid draft" in r.json()["error"]

    # ---- a partial draft: the Register overlay's "Next" has only the connection fields (platform revamp, plan 02 task 5) ----

    @pytest.mark.asyncio
    async def test_stt_test_needs_only_the_connection_fields(self, client, monkeypatch) -> None:
        """Page 1 of the overlay has the endpoint and maybe a key: no default model, no limits, no id."""
        calls = _patch_probe(monkeypatch, "list_models", answer=["whisper-1"])

        r = await client.post(
            "/v1/stt_providers/_test", json={"config": {"url": "http://asr.local:8006/v1"}},
        )

        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "models": ["whisper-1"]}
        assert calls == [{"url": "http://asr.local:8006/v1", "api_key": None}]

    @pytest.mark.asyncio
    async def test_tts_test_needs_only_the_connection_fields(self, client, monkeypatch) -> None:
        calls = _patch_probe(monkeypatch, "list_voices", answer=["af_heart", "am_adam"])

        r = await client.post(
            "/v1/tts_providers/_test", json={"config": {"url": "http://tts.local:8004/v1"}},
        )

        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "voices": ["af_heart", "am_adam"]}
        assert calls == [{"url": "http://tts.local:8004/v1", "api_key": None}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "attr"), _PROBES)
    async def test_the_key_of_a_partial_draft_reaches_the_probe(self, client, monkeypatch, route, attr) -> None:
        """The key is the one secret on page 1: the probe is given it in clear. Naming the kind (``provider: openai``) or leaving it out is the same draft."""
        calls = _patch_probe(monkeypatch, attr, answer=["x"])

        for body in (
            {"config": {"url": "http://svc.local/v1", "api_key": "sk-conn-KEY-1234"}},
            {"provider": "openai", "config": {"url": "http://svc.local/v1", "api_key": "sk-conn-KEY-1234"}},
        ):
            r = await client.post(f"/v1/{route}/_test", json=body)
            assert r.status_code == 200 and r.json()["ok"] is True, r.text

        assert calls == [{"url": "http://svc.local/v1", "api_key": "sk-conn-KEY-1234"}] * 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "attr"), _PROBES)
    async def test_a_draft_is_probed_whatever_its_row_fields_say(self, client, monkeypatch, route, attr) -> None:
        """``id``, ``default_model``, ``default_voice`` and ``limits`` are not the connection: they are not validated by ``_test``, even when the row would refuse them (Save says so)."""
        calls = _patch_probe(monkeypatch, attr, answer=["x"])

        r = await client.post(f"/v1/{route}/_test", json={
            "id": "", "provider": "openai", "default_model": "", "default_voice": "",
            "limits": {"max_concurrency": 0}, "config": {"url": "http://svc.local/v1"},
        })

        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert len(calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "attr"), _PROBES)
    @pytest.mark.parametrize("body", [
        pytest.param({"config": {}}, id="empty-config"),
        pytest.param({}, id="no-config"),
        pytest.param({"config": {"api_key": "sk-only-a-key-1234"}}, id="a-key-and-no-url"),
    ])
    async def test_a_partial_draft_without_a_url_names_the_url(self, client, monkeypatch, route, attr, body) -> None:
        calls = _patch_probe(monkeypatch, attr, answer=["x"])

        r = await client.post(f"/v1/{route}/_test", json=body)

        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] is False and out["error"].startswith("invalid draft"), out
        assert "url" in out["error"], out
        # Only the url is missing as far as this probe goes: the fields it never reads (the person has not got to them yet) are not reported.
        for unread in ("default_model", "default_voice", "limits", "provider"):
            assert unread not in out["error"], out
        assert calls == [], "a draft with no url must not be probed"
        assert "sk-only-a-key-1234" not in r.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "attr"), _PROBES)
    async def test_an_unknown_speech_provider_is_an_invalid_draft(self, client, monkeypatch, route, attr) -> None:
        calls = _patch_probe(monkeypatch, attr, answer=["x"])

        r = await client.post(f"/v1/{route}/_test", json={"provider": "deepgram", "config": {"url": "http://x"}})

        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] is False and out["error"].startswith("invalid draft"), out
        assert "provider" in out["error"] and "openai" in out["error"], f"name the field and what is supported: {out}"
        assert "deepgram" not in out["error"], f"what was typed is not printed back (draft_error's rule): {out}"
        for unread in ("default_model", "default_voice", "limits"):
            assert unread not in out["error"], out
        assert calls == [], "a draft of a kind we do not serve must not be probed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("route", "attr"), _PROBES)
    async def test_a_partial_draft_failure_never_echoes_the_key(self, client, monkeypatch, route, attr) -> None:
        _patch_probe(monkeypatch, attr, error=RuntimeError("401 for sk-speech-SECRET-77"))

        r = await client.post(f"/v1/{route}/_test", json={
            "config": {"url": "http://svc.local/v1", "api_key": "sk-speech-SECRET-77"},
        })

        assert r.status_code == 200, r.text
        error = r.json()["error"]
        # The probe ran and failed (a draft that was refused as invalid would not echo the key either, and would prove nothing).
        assert r.json()["ok"] is False and error.startswith("RuntimeError: 401 for "), error
        assert "sk-speech-SECRET-77" not in r.text
        assert "[REDACTED]" in error, "the key is masked, not dropped silently"


class TestCascadeBlockOnDelete:
    @pytest.mark.asyncio
    async def test_deleting_the_active_stt_provider_is_blocked(self, client) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        await client.put(
            "/v1/speech_active_config", json={"stt_provider_id": "stt-a"},
        )
        r = await client.delete("/v1/stt_providers/stt-a")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["extensions"]["error"] == "cascade_blocked"
        assert body["extensions"]["referenced_by"] == "_active_speech_config"

    @pytest.mark.asyncio
    async def test_deleting_the_active_tts_provider_is_blocked(self, client) -> None:
        await client.post("/v1/tts_providers", json=_TTS)
        await client.put(
            "/v1/speech_active_config", json={"tts_provider_id": "tts-a"},
        )
        r = await client.delete("/v1/tts_providers/tts-a")
        assert r.status_code == 409, r.text


class TestRegistryInvalidation:
    @pytest.mark.asyncio
    async def test_updating_a_row_invalidates_its_cached_adapter(self, client, app) -> None:
        await client.post("/v1/stt_providers", json=_STT)
        adapter = await app.state.stt_registry.get("stt-a")
        updated = dict(_STT, default_model="whisper-large")
        r = await client.put("/v1/stt_providers/stt-a", json=updated)
        assert r.status_code == 200, r.text
        assert await app.state.stt_registry.get("stt-a") is not adapter
