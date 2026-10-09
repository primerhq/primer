"""Migration 8: embedding provider rows the provider-keyed validator refuses are repaired in place (review of #645, B1).

``EmbeddingProvider.config`` is a plain union. Before #645 pydantic picked the member that fitted the dict best, whatever ``provider`` said, so main stored three shapes the
provider-keyed validator now refuses: an ``openai`` row with a missing or invalid url (it validated as a ``GoogleConfig`` with the url dropped: ``{"api_key": ...}``), a
``huggingface`` row with only a url (an ``OpenAIConfig``), and an ``openai`` row with only a token (a ``HuggingFaceConfig``). ``Storage._from_row`` validates uncaught, so one such
row answered 500 on the whole list and on get, put and delete by id (the operator could not even delete it), and each 500 logged pydantic's ``input_value`` with most of the
stored key.

The rows below are the JSON main wrote, seeded through a class that reads the config untyped (the table is selected by the class NAME, as in m007's tests).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ConfigDict, ValidationError

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.provider import SqliteConfig
from primer.model.providers.embedding import EmbeddingProvider as LiveEmbeddingProvider
from primer.model.providers.embedding import HuggingFaceConfig, OpenAIConfig
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider

KEY = "sk-secret-0123456789abcdefghijklmnopqrstuvwxyz"
TOKEN = "hf_secret_0123456789abcdefghijklmnopqrstuvwxyz"
URL = "http://emb.local:1234/v1"


class EmbeddingProvider(Identifiable):  # noqa: N801 - the class name selects the storage table
    """A row as main stored it: ``config`` untyped."""

    model_config = ConfigDict(extra="allow")

    provider: str
    config: dict[str, Any]


def _row(row_id: str, provider: str, config: dict[str, Any]) -> EmbeddingProvider:
    return EmbeddingProvider(id=row_id, provider=provider, config=config, models=[{"name": "m-1"}], limits={"max_concurrency": 2})


BROKEN = {
    "openai-no-url": _row("openai-no-url", "openai", {"api_key": KEY}),
    "openai-token-only": _row("openai-token-only", "openai", {"token": TOKEN}),
}
HEALTHY = {
    # Main stored this for a huggingface row with only a url (an OpenAIConfig). The HuggingFace token is optional now (a public local model needs none), so the live model reads
    # it, ignoring the stray keys, and the migration has nothing to repair.
    "hf-url-only": _row("hf-url-only", "huggingface", {"url": URL, "api_key": KEY, "flavor": "other"}),
    "openai-ok": _row("openai-ok", "openai", {"url": URL, "api_key": KEY, "flavor": "lmstudio"}),
    "hf-ok": _row("hf-ok", "huggingface", {"token": TOKEN}),
    "gemini-ok": _row("gemini-ok", "gemini", {"api_key": KEY}),
}


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "m008.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _seed(sp: StorageProvider) -> None:
    legacy = sp.get_storage(EmbeddingProvider)
    for row in (*BROKEN.values(), *HEALTHY.values()):
        await legacy.create(row)


async def _raw(sp: StorageProvider) -> dict[str, dict[str, Any]]:
    page = await sp.get_storage(EmbeddingProvider).list(OffsetPage(offset=0, length=100))
    return {row.id: row.model_dump() for row in page.items}


def _migration():
    from primer.storage.migrations.m008_embedding_provider_repair import M008EmbeddingProviderRepair

    return M008EmbeddingProviderRepair()


# ---- the seeding reproduces the bug (a guard against a vacuous test) -------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("row_id", sorted(BROKEN))
async def test_main_stored_rows_the_live_model_cannot_read(sp: StorageProvider, row_id: str) -> None:
    await _seed(sp)

    with pytest.raises(ValidationError):
        await sp.get_storage(LiveEmbeddingProvider).get(row_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("row_id", sorted(HEALTHY))
async def test_the_healthy_rows_read_fine(sp: StorageProvider, row_id: str) -> None:
    await _seed(sp)

    assert (await sp.get_storage(LiveEmbeddingProvider).get(row_id)).id == row_id


# ---- the repair --------------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_row_reads_with_the_live_model_after_the_migration(sp: StorageProvider) -> None:
    await _seed(sp)

    await _migration().apply(sp)

    page = await sp.get_storage(LiveEmbeddingProvider).list(OffsetPage(offset=0, length=100))
    assert {row.id for row in page.items} == set(BROKEN) | set(HEALTHY)


@pytest.mark.asyncio
async def test_a_huggingface_row_of_main_s_shape_is_left_as_it_is(sp: StorageProvider) -> None:
    """The first version of the migration gave it ``{"token": ""}``. It reads fine without (the token is optional), and a migration should not rewrite what is not broken."""
    await _seed(sp)
    before = (await _raw(sp))["hf-url-only"]

    await _migration().apply(sp)

    assert (await _raw(sp))["hf-url-only"] == before
    assert (await sp.get_storage(LiveEmbeddingProvider).get("hf-url-only")).provider.value == "huggingface"


@pytest.mark.asyncio
async def test_an_openai_row_with_no_url_keeps_its_key_and_gets_a_url_that_cannot_resolve(sp: StorageProvider) -> None:
    """There is no endpoint to restore. The row is kept (its id may be named by a collection, and deleting an operator's row on boot is not ours to do) with a base URL on the
    reserved ``.invalid`` domain, which says what to fix and fails loudly if used; the key is kept as it was stored."""
    await _seed(sp)

    await _migration().apply(sp)

    row = await sp.get_storage(LiveEmbeddingProvider).get("openai-no-url")
    assert type(row.config) is OpenAIConfig
    assert row.config.url.host is not None and row.config.url.host.endswith(".invalid"), row.config.url
    assert row.config.url.scheme == "https", "the kept key would travel as a bearer header: over plain http to whatever answers that name"
    assert row.config.api_key is not None and row.config.api_key.get_secret_value() == KEY
    assert (await _raw(sp))["openai-no-url"]["config"]["api_key"] == KEY, "stored in the clear, as every row is"


@pytest.mark.asyncio
async def test_an_openai_row_with_only_a_token_gets_the_same_url_and_loses_the_token(sp: StorageProvider) -> None:
    """A HuggingFace token is not an OpenAI key, so it is not carried over."""
    await _seed(sp)

    await _migration().apply(sp)

    config = (await _raw(sp))["openai-token-only"]["config"]
    assert "token" not in config and TOKEN not in str(config)
    assert str(config["url"]).startswith("https://") and ".invalid" in str(config["url"])


@pytest.mark.asyncio
async def test_a_healthy_row_is_not_touched(sp: StorageProvider) -> None:
    await _seed(sp)
    before = await _raw(sp)

    await _migration().apply(sp)

    after = await _raw(sp)
    assert {k: after[k] for k in HEALTHY} == {k: before[k] for k in HEALTHY}


@pytest.mark.asyncio
async def test_a_repaired_row_keeps_its_id_its_models_and_its_limits(sp: StorageProvider) -> None:
    await _seed(sp)

    await _migration().apply(sp)

    after = await _raw(sp)
    for row_id, row in BROKEN.items():
        assert after[row_id]["provider"] == row.provider
        assert after[row_id]["models"] == [{"name": "m-1"}] and after[row_id]["limits"] == {"max_concurrency": 2}


@pytest.mark.asyncio
async def test_running_it_twice_changes_nothing_the_second_time(sp: StorageProvider) -> None:
    await _seed(sp)
    await _migration().apply(sp)
    once = await _raw(sp)

    await _migration().apply(sp)

    assert await _raw(sp) == once


@pytest.mark.asyncio
async def test_a_database_with_no_embedding_provider_is_a_no_op(sp: StorageProvider) -> None:
    await _migration().apply(sp)

    assert await _raw(sp) == {}


@pytest.mark.asyncio
async def test_the_placeholder_is_https_on_the_reserved_invalid_domain() -> None:
    """The row keeps its stored api_key, and the first use sends ``Authorization: Bearer <key>``. Over plain http that goes to whatever resolves the name (a search list, an
    NXDOMAIN-hijacking resolver); over https no CA can issue a certificate for ``.invalid``, so the TLS handshake fails before any header is sent."""
    from primer.storage.migrations.m008_embedding_provider_repair import PLACEHOLDER_URL

    assert PLACEHOLDER_URL == "https://base-url-not-set.invalid/"


# ---- what it says about it ---------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_log_names_the_id_and_the_provider_and_never_the_config(sp: StorageProvider, caplog: pytest.LogCaptureFixture) -> None:
    await _seed(sp)

    with caplog.at_level(logging.DEBUG, logger="primer"):
        await _migration().apply(sp)

    # Primer's own records: a third-party DEBUG logger (aiosqlite prints every statement with its data) is not this migration's log.
    ours = [record for record in caplog.records if record.name.startswith("primer.")]
    everything = " ".join(record.getMessage() + " " + str(record.__dict__) for record in ours)
    assert ours, "the repair says what it did"
    for secret in (KEY, TOKEN, URL, "sk-secret", "hf_secret"):
        assert secret not in everything, f"the log carries {secret!r}"
    repaired = {getattr(record, "provider_id", None): getattr(record, "provider", None) for record in ours if getattr(record, "provider_id", None)}
    assert repaired == {"openai-no-url": "openai", "openai-token-only": "openai"}


# ---- it is registered, and the runner applies it ----------------------------------------------------------------------------------------------------------------------


def test_the_migration_is_the_latest_registered_one() -> None:
    from primer.storage.migrations import LATEST_VERSION, MIGRATIONS

    assert _migration().version == 8 == LATEST_VERSION
    assert MIGRATIONS[-1].version == 8 and type(MIGRATIONS[-1]).__name__ == "M008EmbeddingProviderRepair"


@pytest.mark.asyncio
async def test_the_runner_applies_it_to_a_database_at_version_seven(sp: StorageProvider) -> None:
    from primer.storage.migrations import run_migrations

    await _seed(sp)
    await sp.set_schema_version(7)

    version = await run_migrations(sp, is_fresh_install=False)

    assert version == 8 and (await sp.get_system_state()).schema_version == 8
    page = await sp.get_storage(LiveEmbeddingProvider).list(OffsetPage(offset=0, length=100))
    assert len(page.items) == len(BROKEN) + len(HEALTHY)
