"""ApiToken model + token helpers — Spec §3, §4."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from primer.model.api_token import ApiToken, SCOPE_MCP, KNOWN_SCOPES
from primer.auth.api_tokens import (
    PLAINTEXT_PREFIX,
    mint_plaintext,
    hash_token,
    extract_prefix,
)


def test_mint_plaintext_has_prefix_and_is_unique():
    a = mint_plaintext()
    b = mint_plaintext()
    assert a.startswith(PLAINTEXT_PREFIX)
    assert b.startswith(PLAINTEXT_PREFIX)
    assert a != b
    assert len(a) > 40  # primer_pat_ + 32 bytes base64url


def test_hash_token_is_deterministic_sha256():
    t = "primer_pat_abc"
    a = hash_token(t)
    b = hash_token(t)
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_extract_prefix_returns_8_chars():
    assert extract_prefix("primer_pat_abcdefghij") == "primer_p"


def test_api_token_model_round_trips():
    t = ApiToken(
        id="at-x",
        user_id="u-1",
        name="claude-desktop",
        token_hash="a" * 64,
        prefix="primer_p",
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    dumped = t.model_dump()
    rehydrated = ApiToken.model_validate(dumped)
    assert rehydrated.scopes == ["mcp"]


def test_api_token_name_validator_strips_and_rejects_empty():
    with pytest.raises(ValidationError):
        ApiToken(
            id="at-x",
            user_id="u-1",
            name="   ",
            token_hash="a" * 64,
            prefix="primer_p",
            created_at=datetime.now(timezone.utc),
        )


def test_api_token_scopes_dedup_and_lowercase():
    t = ApiToken(
        id="at-x",
        user_id="u-1",
        name="x",
        token_hash="a" * 64,
        prefix="primer_p",
        scopes=["MCP", "mcp", "  API  ", ""],
        created_at=datetime.now(timezone.utc),
    )
    assert t.scopes == ["mcp", "api"]


def test_scope_mcp_constant_exists():
    assert SCOPE_MCP == "mcp"
    assert SCOPE_MCP in KNOWN_SCOPES
