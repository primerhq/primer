"""Unit tests for the argon2 password helpers."""

from __future__ import annotations

import pytest

from primer.auth.passwords import hash_password, verify_password


@pytest.mark.asyncio
async def test_hash_then_verify_succeeds():
    h = await hash_password("correct horse battery staple")
    assert h.startswith("$argon2")
    assert await verify_password("correct horse battery staple", h) is True


@pytest.mark.asyncio
async def test_verify_wrong_password_returns_false():
    h = await hash_password("the-right-one")
    assert await verify_password("the-wrong-one", h) is False


@pytest.mark.asyncio
async def test_verify_malformed_hash_returns_false():
    assert await verify_password("any", "not-a-valid-hash") is False
    assert await verify_password("any", "") is False


@pytest.mark.asyncio
async def test_hashes_are_salted_unique():
    """Same plaintext → different stored hashes (different salts)."""
    a = await hash_password("same-password")
    b = await hash_password("same-password")
    assert a != b
    assert await verify_password("same-password", a) is True
    assert await verify_password("same-password", b) is True


@pytest.mark.asyncio
async def test_verify_none_hash_returns_false():
    """A None stored_hash (account provisioned without a password) returns
    False without raising — so a password-less row can never be logged into
    and the login endpoint returns 401, not 500."""
    assert await verify_password("any", None) is False
