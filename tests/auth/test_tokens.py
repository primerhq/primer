"""Unit tests for session token sign/verify."""

from __future__ import annotations

import time

import pytest

from primer.auth.tokens import SessionPayload, sign_session, verify_session


def test_round_trip():
    secret = "test-secret-32-bytes" + "_" * 16
    token = sign_session(user_id="u1", username="alice", secret=secret)
    payload = verify_session(token=token, secret=secret, max_age_seconds=60)
    assert payload == SessionPayload(user_id="u1", username="alice")


def test_wrong_secret_rejects():
    token = sign_session(user_id="u1", username="alice", secret="secret-A")
    payload = verify_session(token=token, secret="secret-B", max_age_seconds=60)
    assert payload is None


def test_expired_token_rejects():
    secret = "x" * 32
    token = sign_session(user_id="u1", username="alice", secret=secret)
    # itsdangerous timestamps are second-resolution; sleep well past the
    # boundary so the comparison clearly exceeds max_age.
    time.sleep(2.05)
    payload = verify_session(token=token, secret=secret, max_age_seconds=1)
    assert payload is None


def test_empty_token_rejects():
    assert verify_session(token="", secret="s", max_age_seconds=60) is None


def test_garbage_token_rejects():
    assert verify_session(token="not-a-token", secret="s", max_age_seconds=60) is None
