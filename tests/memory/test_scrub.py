"""The deterministic scrubber (agent memory T4): a delta with any personal-data hit is dropped, never repaired."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from primer.memory.scrub import known_names, personal_data_hits
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.storage.sqlite import SqliteStorageProvider

# Token-shaped strings are assembled here, not stored literally: GitHub push protection refuses a commit that holds one, fake or not.
_TOKENS = {
    "SK_TOKEN": "sk" + "-" + "abcdefghijklmnopqrstuvwx1234",
    "GH_TOKEN": "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "SLACK_TOKEN": "xo" + "xb-" + "123456789012-abcdefghijklmnop",
    "AWS_KEY_ID": "AK" + "IA" + "IOSFODNN7EXAMPLE",
}
CORPUS = [
    line.format(**_TOKENS)
    for line in (Path(__file__).parent / "scrub_corpus.txt").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
NAMES = ["zoltan", "maria.lopez"]
PROVIDER = SimpleNamespace(config=SimpleNamespace(api_key=SecretStr("provider-secret-9f8e7d6c5b4a")))


@pytest.mark.parametrize("line", CORPUS)
def test_corpus_every_line_is_caught(line):
    assert personal_data_hits(line, known_names=NAMES, provider=PROVIDER), line


@pytest.mark.parametrize("text", [
    "When the API answers 429, wait for the retry-after header before retrying.",
    "Prefer the <repo> tests directory; ask the user which <customer id> to use.",
    "Step 1: open the page. Step 2: retry at 12:30:45 only if the job is idle.",
    "Upgrade from 1.2.3 to 1.4.0 before enabling the cache; the workspace template needs a refresh.",
    "Use the workspace-template list to pick a base image.",
    "",
])
def test_clean_generalized_lesson_has_no_hits(text):
    assert personal_data_hits(text, known_names=NAMES, provider=PROVIDER) == []


def test_the_classes_are_named():
    assert personal_data_hits("mail a@b.io", known_names=[]) == ["email"]
    assert "known_name" in personal_data_hits("ask Zoltan", known_names=NAMES)
    assert "ip" in personal_data_hits("host 10.1.2.3", known_names=[])
    assert "url_credentials" in personal_data_hits("https://u:p@h.example/x", known_names=[])
    assert "workspace_id" in personal_data_hits("see ws-3f9a1c8d5b2e4a67", known_names=[])
    assert "session_id" in personal_data_hits("see sess-3f9a1c8d5b2e4a67", known_names=[])
    assert "credential" in personal_data_hits("the key is provider-secret-9f8e7d6c5b4a", known_names=[], provider=PROVIDER)


def test_a_short_or_blank_name_never_blanks_ordinary_words():
    assert personal_data_hits("wait a moment, then retry", known_names=["a", "", "mo"]) == []


def test_a_control_character_does_not_hide_a_secret():
    assert personal_data_hits("key provider-secret-9f8e7\x00d6c5b4a here", known_names=[], provider=PROVIDER)


def test_a_long_text_is_checked_to_the_end():
    assert "email" in personal_data_hits("lesson. " * 2000 + "mail a@b.io", known_names=[])


async def test_known_names_are_usernames_and_email_local_parts(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        users = sp.get_storage(User)
        now = datetime.now(UTC)
        await users.create(User(id="u1", username="zoltan", email="Maria.Lopez@example.com", created_at=now, role="user"))
        await users.create(User(id="u2", username="bo", created_at=now, role="user"))
        names = await known_names(sp)
        assert {"zoltan", "maria.lopez", "bo"} <= {n.lower() for n in names}
    finally:
        await sp.aclose()
