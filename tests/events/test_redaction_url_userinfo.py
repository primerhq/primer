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


@pytest.mark.parametrize(
    ("text", "secrets", "kept"),
    [
        pytest.param("http://svc:pw1@h/v1 failed; fallback http://svc2:pw2@i/v1 failed too", ("pw1", "pw2"), "http://svc:**********@h/v1 failed", id="a leaf that begins with a URL and holds a second one"),
        pytest.param("http://x said hi to bob@example.com, see http://u:pw2@h/", ("pw2",), "bob@example.com", id="free text that begins with a scheme, an e-mail address and then a URL"),
        pytest.param("http://u:p1@h/?next=http://v:p2@i/", ("p1", "p2"), "http://u:**********@h/?next=", id="two URLs with no whitespace between them"),
        pytest.param("https://u:pw1@h/x?token=tok2", ("pw1", "tok2"), "https://u:**********@h/x?token=", id="a URL with userinfo and a token in its query"),
    ],
)
def test_a_leaf_that_begins_with_a_credentialed_url_does_not_keep_a_second_secret(text: str, secrets: tuple[str, ...], kept: str) -> None:
    """``redact_payload`` returned the served-row mask as soon as ``mask_userinfo`` changed a leaf, so whatever followed the first URL (a second URL, a query token) stayed in clear (review of #691)."""
    out = redact_payload({"m": text})["m"]

    assert not [s for s in secrets if s in out], out
    assert kept in out, out


@pytest.mark.parametrize(
    ("text", "secrets", "kept"),
    [
        pytest.param("https://bot:correct horse battery@mcp.example/mcp", ("correct", "horse", "battery"), "https://bot:**********@mcp.example/mcp", id="a password with spaces"),
        pytest.param("https://bot:pass\tword@mcp.example/mcp", ("pass", "word"), "https://bot:**********@mcp.example/mcp", id="a password with a tab"),
        pytest.param("https://u:p ss@host.example and https://u3:p3@h3/", ("p ss", "p3"), "https://u:**********@host.example and ", id="a password with a space and then a second URL"),
        pytest.param("https://u:p ss@host.example/x?token=tok9", ("p ss", "tok9"), "https://u:**********@host.example/x?token=", id="a password with a space and a query token"),
        # a raw "@" BEFORE the whitespace made the whitespace-free lead hold an "@", so the lead branch ran first and the tail after the whitespace stayed in clear (round 2 review, B2r)
        pytest.param("https://u:p@ss word@host.example/x", ("p@ss", "ss word", "word@"), "https://u:**********@host.example/x", id="a raw @ and then a space in the password"),
        pytest.param("https://u:p@ss\tword@host.example/x", ("p@ss", "ss\tword", "word@"), "https://u:**********@host.example/x", id="a raw @ and then a tab in the password"),
    ],
)
def test_a_url_password_that_holds_whitespace_is_masked(text: str, secrets: tuple[str, ...], kept: str) -> None:
    """``httpx`` accepts such a URL (it percent-encodes the userinfo), so a working MCP ``url`` or Kubernetes ``apiserver_url`` (plain strings) can carry one into a CRUD event. Main masked it
    because ``mask_userinfo`` reads the authority up to the first ``/ ? #``; reading only a whitespace-free lead let it through (review of #711, round 1)."""
    out = redact_payload({"leaf": text})["leaf"]

    assert not [s for s in secrets if s in out], out
    assert kept in out, out


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        pytest.param("https://user:@host.example/x", "https://**********@host.example/x", id="an empty password"),
        pytest.param("https://ghp_abcdefghij@github.com/org/repo failed again", "https://**********@github.com/org/repo failed again", id="a lone token"),
    ],
)
def test_the_whitespace_reading_that_runs_first_leaves_a_lone_userinfo_to_the_whole_mask(text: str, kept: str) -> None:
    """Round 3 runs the whitespace-aware reading before the whitespace-free lead. It must not take over a userinfo with no password: the username is then the only candidate secret and is masked whole
    (the rule of ``mask_userinfo``), which a ``user:**********`` rewrite would have shown."""
    out = redact_payload({"leaf": text})["leaf"]

    assert out == kept, out


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("http://svc error: write to admin@example.com", id="prose that begins with a scheme and holds an e-mail address"),
        pytest.param("https://host.example and mail me: bob@x.com about it", id="a host, then words with a colon and an e-mail address"),
    ],
)
def test_prose_that_begins_with_a_scheme_is_not_taken_for_a_userinfo(text: str) -> None:
    """The user of a ``user:password`` is a single token: a space before the first colon means it is a sentence, which stays as it is."""
    assert redact_payload({"leaf": text})["leaf"] == text


def test_linear_on_a_large_adversarial_leaf() -> None:
    import time

    started = time.perf_counter()
    redact_payload({"m": "http://" + "a" * 1_000_000 + "@"})
    redact_payload({"m": ("a." * 500_000) + "@x ://"})
    redact_payload({"m": "http://" + "a:" * 500_000 + " b@"})
    redact_payload({"m": "http://u:" + "p " * 500_000 + "@h/"})

    assert time.perf_counter() - started < 5.0
