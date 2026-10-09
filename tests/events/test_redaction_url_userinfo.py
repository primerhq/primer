"""CRUD event payloads do not carry a provider Base URL's password (ticket 01a11cdf part 3, option A).

A CRUD event carries the stored row's own dump, and storage unwraps ``SecretStr`` to plaintext before persisting, so the event holds the real ``config.url`` of a provider with
``user:password@`` in it. ``redact_payload`` masks by KEY NAME and by the model registry of ``SecretStr`` fields, and a ``url`` is neither, so it passed through. Every event read now
masks the password of a string leaf that is a URL with userinfo (the same helper as the served ``config.url``): the username stays when there is a password, a lone userinfo goes whole.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.common.url_userinfo import MASK
from primer.events.redaction import redact_event, redact_payload
from primer.events.registry import register_event_kind
from primer.model.provider import SqliteConfig
from primer.model.providers.llm import LLMProvider
from primer.storage.sqlite import SqliteStorageProvider

PROXY = "http://svc:s3cr3t@proxy.local:8080/v1"
MASKED = f"http://svc:{MASK}@proxy.local:8080/v1"


def test_a_url_leaf_with_a_password_is_masked_wherever_it_sits() -> None:
    out = redact_payload({
        "id": "llm-a",
        "config": {"url": PROXY, "flavor": "other"},
        "mirrors": [PROXY, "http://plain.local/v1"],
        "nested": {"deeper": {"git_url": "https://ghp_abcdefghij@github.com/org/repo.git"}},
    })

    assert out["config"] == {"url": MASKED, "flavor": "other"}
    assert out["mirrors"] == [MASKED, "http://plain.local/v1"]
    assert out["nested"]["deeper"]["git_url"] == f"https://{MASK}@github.com/org/repo.git"


@pytest.mark.parametrize(
    "value",
    ["http://plain.local/v1", "not a url", "", "user@example.com", "http://host/a@b", 3, None, True],
)
def test_anything_else_passes_through_unchanged(value) -> None:
    assert redact_payload({"x": value}) == {"x": value}


def test_the_input_is_not_modified() -> None:
    payload = {"config": {"url": PROXY}}

    redact_payload(payload)

    assert payload == {"config": {"url": PROXY}}


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    register_event_kind("llmprovider", LLMProvider)
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_a_crud_event_of_a_credentialed_provider_is_served_masked_and_stored_real(sp: SqliteStorageProvider) -> None:
    row = LLMProvider.model_validate({
        "id": "llm-a", "provider": "openchat", "models": [{"name": "m", "context_length": 8192}],
        "config": {"url": PROXY, "api_key": "sk-live-abcdef", "flavor": "other"}, "limits": {"max_concurrency": 1},
    })
    await sp.get_storage(LLMProvider).create(row)

    events = await sp.get_event_store().read_after(0)

    created = next(e for e in events if e.event_type.endswith(".created"))
    assert created.payload["config"]["url"] == PROXY, "the event store keeps what storage persisted"
    served = redact_event(created).payload
    assert served["config"]["url"] == MASKED and "s3cr3t" not in str(served), served
    assert served["config"]["api_key"] != "sk-live-abcdef"


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("see http://svc:pw@host/ for details", "see http://[REDACTED]@host/ for details"),
        ("connect to https://ghp_abcdefghij@github.com/org/repo.git failed: refused", "connect to https://[REDACTED]@github.com/org/repo.git failed: refused"),
        ("a http://u:p@one.local/x and https://v:q@two.local/y", "a http://[REDACTED]@one.local/x and https://[REDACTED]@two.local/y"),
    ],
)
def test_a_credentialed_url_inside_free_text_is_masked_too(text: str, masked: str) -> None:
    """Only a string that IS a URL used the served-row mask; a credentialed URL inside a sentence (an error text, a log line copied into an event) passed. It goes through
    ``redact_url_secrets`` (linear, handles embedded URLs), whose mask is ``[REDACTED]``."""
    assert redact_payload({"message": text}) == {"message": masked}
