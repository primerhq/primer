import pytest

from primectl.client import ApiError, ConnectionFailed
from primectl.errors import exit_code_for, format_error, EXIT_NOT_FOUND, EXIT_CONFLICT, EXIT_GENERAL


def test_not_found_exit_code():
    err = ApiError(404, {"detail": "nope"}, "")
    assert exit_code_for(err) == EXIT_NOT_FOUND


def test_conflict_exit_code():
    err = ApiError(409, {"detail": "dup"}, "")
    assert exit_code_for(err) == EXIT_CONFLICT


def test_other_status_general_exit_code():
    err = ApiError(422, {"detail": "bad"}, "")
    assert exit_code_for(err) == EXIT_GENERAL


def test_connection_failed_exit_code():
    assert exit_code_for(ConnectionFailed("x")) == EXIT_GENERAL


def test_format_401_includes_auth_hint():
    msg = format_error(ApiError(401, {"title": "Unauthorized"}, ""))
    assert "not authenticated" in msg.lower()
    assert "token" in msg.lower()


def test_format_connection_failed_mentions_server():
    msg = format_error(ConnectionFailed("Connection refused"), server="http://localhost:9000")
    assert "http://localhost:9000" in msg


def test_format_validation_shows_detail():
    msg = format_error(ApiError(422, {"detail": "field x required"}, ""))
    assert "field x required" in msg
