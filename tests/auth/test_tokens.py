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


def test_a_verified_session_says_when_it_was_issued():
    """The open-connection watcher needs the cookie's own issue time to end a connection when the cookie's lifetime does."""
    from datetime import datetime, timezone

    secret = "x" * 32
    before = datetime.now(timezone.utc).replace(microsecond=0)
    payload = verify_session(token=sign_session(user_id="u1", username="alice", secret=secret), secret=secret, max_age_seconds=60)
    after = datetime.now(timezone.utc)
    assert payload is not None and payload.issued_at is not None
    assert payload.issued_at.tzinfo is not None
    assert before <= payload.issued_at <= after


def test_the_issue_time_is_not_part_of_what_a_payload_equals():
    from datetime import datetime, timezone

    assert SessionPayload(user_id="u1", username="alice") == SessionPayload(
        user_id="u1", username="alice", issued_at=datetime.now(timezone.utc),
    )
