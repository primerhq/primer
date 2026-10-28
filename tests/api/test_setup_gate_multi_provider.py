"""G13-09: the setup gate's "LLM provider responds" predicate judges the
provider the DEFAULT profile names, not whichever row the storage lists first.

The stuck shape this closes (audit G13, finding G13-09): a multi-provider
install where the first-listed row is dead but a later row is the one the
default profile (``default_profile_id`` - what the seeded agents bind to)
runs under. The old first-row-only probe held "Enter Primer" out on a
provider that is not the deployment's default, and the wizard offers no way
past it (it can create/update rows, never delete or reorder).

The probe is monkeypatched (the module-level name setup.py imported) so none
of these tests needs a network, a real LLM, or Postgres: the neighbouring
setup-state tests run on the in-memory ``client``/``app`` fixtures, and the
fake storage's list order is insertion order, so "first row" is exactly the
row created first here.
"""
from __future__ import annotations

from primer.model.except_ import BadRequestError
from primer.model.model_profile import ModelProfile
from primer.model.provider import (
    AnthropicConfig,
    LLMProvider,
    LLMProviderType,
)
from primer.model.providers._shared import Limits


def _provider(provider_id: str) -> LLMProvider:
    return LLMProvider(
        id=provider_id,
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key="sk-not-a-real-key"),
        limits=Limits(max_concurrency=1),
    )


def _fake_probe(healthy_ids: set[str]):
    """The module-level stand-in for discover_saved_llm_models: it answers
    for the named rows and fails (like the real probe's refusals) for the rest."""

    async def fake_discover(provider_id: str, providers=None) -> dict:
        if provider_id in healthy_ids:
            return {"models": [{"name": "m1"}]}
        raise BadRequestError(f"{provider_id} probe failed: 401 unauthorized")

    return fake_discover


async def test_dead_first_row_with_healthy_default_later_row_clears_the_predicate(
    client, app, monkeypatch,
):
    """The stuck shape: the dead row is listed FIRST; the default profile
    (the fake storage seeds "p--m" first, provider "p") names the healthy
    LATER row. The predicate must pass, and the gate's complete flag must no
    longer be held by the llm_provider row."""
    import primer.api.routers.setup as setup_router

    monkeypatch.setattr(
        setup_router, "discover_saved_llm_models", _fake_probe({"p"}),
    )
    providers = app.state.storage_provider.get_storage(LLMProvider)
    # Insertion order is the storage's list order here: the dead row is first.
    await providers.create(_provider("zz-dead-first"))
    await providers.create(_provider("p"))

    r = await client.get("/v1/setup/state")
    assert r.status_code == 200, r.text
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"]["ok"] is True
    assert by_key["llm_provider"]["live"] is True
    assert by_key["llm_provider"]["detail"] is None
    assert "llm_provider" not in r.json()["missing"]


async def test_the_default_profiles_provider_is_the_one_probed(client, app, monkeypatch):
    """A dead row the default profile names fails the predicate even though
    an earlier-listed row is healthy: the profile's provider is the judgment,
    not the list position."""
    import primer.api.routers.setup as setup_router

    monkeypatch.setattr(
        setup_router, "discover_saved_llm_models", _fake_probe({"solo-healthy"}),
    )
    providers = app.state.storage_provider.get_storage(LLMProvider)
    await providers.create(_provider("solo-healthy"))
    await providers.create(_provider("p"))  # "p" is the default profile's provider

    r = await client.get("/v1/setup/state")
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"]["ok"] is False
    assert by_key["llm_provider"]["live"] is True
    assert "p probe failed" in by_key["llm_provider"]["detail"]


async def test_single_healthy_provider_still_passes(client, app, monkeypatch):
    """The single-provider fresh-install case (wizard-recoverable, the
    common shape) keeps passing."""
    import primer.api.routers.setup as setup_router

    monkeypatch.setattr(
        setup_router, "discover_saved_llm_models", _fake_probe({"solo"}),
    )
    await app.state.storage_provider.get_storage(LLMProvider).create(_provider("solo"))

    r = await client.get("/v1/setup/state")
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"] == {
        "key": "llm_provider", "label": "LLM provider responds",
        "ok": True, "live": True, "detail": None,
    }


async def test_single_dead_provider_still_fails(client, app, monkeypatch):
    """A deployment whose only provider is dead still reads as not
    responding, with the probe's failure as the detail."""
    import primer.api.routers.setup as setup_router

    monkeypatch.setattr(
        setup_router, "discover_saved_llm_models", _fake_probe(set()),
    )
    await app.state.storage_provider.get_storage(LLMProvider).create(_provider("solo"))

    r = await client.get("/v1/setup/state")
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"]["ok"] is False
    assert by_key["llm_provider"]["live"] is True
    assert "solo probe failed" in by_key["llm_provider"]["detail"]


async def test_no_profile_yet_falls_back_to_probing_every_row(client, app, monkeypatch):
    """Mid-wizard (no ModelProfile row exists yet) there is no default to
    name: the probe walks the rows, capped, and one answering row clears the
    predicate."""
    import primer.api.routers.setup as setup_router

    monkeypatch.setattr(
        setup_router, "discover_saved_llm_models", _fake_probe({"second-alive"}),
    )
    profiles = app.state.storage_provider.get_storage(ModelProfile)
    assert profiles._data  # the fake storage's standard vocabulary
    profiles._data.clear()

    providers = app.state.storage_provider.get_storage(LLMProvider)
    await providers.create(_provider("zz-dead-first"))
    await providers.create(_provider("second-alive"))

    r = await client.get("/v1/setup/state")
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"]["ok"] is True
    assert by_key["llm_provider"]["live"] is True


async def test_a_profile_naming_no_stored_row_falls_back_to_the_rows(client, app, monkeypatch):
    """The default profile names a provider row that is not stored (the
    row was deleted out from under it): the first row is not enough to
    answer for the deployment, so the probe walks the rows."""
    import primer.api.routers.setup as setup_router

    monkeypatch.setattr(
        setup_router, "discover_saved_llm_models", _fake_probe({"second-alive"}),
    )
    providers = app.state.storage_provider.get_storage(LLMProvider)
    await providers.create(_provider("zz-dead-first"))
    await providers.create(_provider("second-alive"))
    # The default profile ("p--m", seeded first) names provider "p": no such
    # row is stored here.
    r = await client.get("/v1/setup/state")
    by_key = {p["key"]: p for p in r.json()["predicates"]}
    assert by_key["llm_provider"]["ok"] is True
    assert by_key["llm_provider"]["live"] is True
