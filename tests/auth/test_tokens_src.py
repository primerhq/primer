"""Unit tests for the ``src`` session claim (Layer 2 task 5)."""

from __future__ import annotations

from itsdangerous import URLSafeTimedSerializer

from primer.auth.tokens import SessionPayload, sign_session, verify_session


def test_src_round_trips():
    secret = "test-secret-32-bytes" + "_" * 16
    token = sign_session(
        user_id="u1", username="alice", secret=secret, src="oidc-provider-1",
    )
    payload = verify_session(token=token, secret=secret, max_age_seconds=60)
    assert payload == SessionPayload(
        user_id="u1", username="alice", src="oidc-provider-1",
    )


def test_src_defaults_to_local():
    secret = "test-secret-32-bytes" + "_" * 16
    token = sign_session(user_id="u1", username="alice", secret=secret)
    payload = verify_session(token=token, secret=secret, max_age_seconds=60)
    assert payload.src == "local"


def test_legacy_token_without_src_defaults_to_local():
    """A token signed before `src` existed (raw {uid, username} dict, no
    `src` key) must still verify cleanly with src="local" — no crash."""
    secret = "test-secret-32-bytes" + "_" * 16
    # Simulate the pre-Layer-2 payload shape directly (bypassing
    # sign_session, which always includes `src` now).
    s = URLSafeTimedSerializer(secret, salt="primer.session.v1")
    legacy_token = s.dumps({"uid": "u1", "username": "alice"})
    payload = verify_session(token=legacy_token, secret=secret, max_age_seconds=60)
    assert payload == SessionPayload(user_id="u1", username="alice", src="local")
